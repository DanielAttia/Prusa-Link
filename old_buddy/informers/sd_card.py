"""
I'll try to explain myself here
The SD state can start only in the UNSURE state, we know nothing

From there, we will ask the printer about the files present.
If there are files, the SD card is present.
If not, we still know nothing and need to ask the printer to r-init the card
that provides the information about SD card presence

The same situation arises when the user inserts a card.
We get into the INITIALISING state.
The card could have been removed immediately after insertion, it could have
been empty, or full of files. Normally inserted card with files is easy.
We'll see files. If there are no files, the re-init tells us the truth
- If we determined, the card is present, let's tell Connect.

Now the card removal is tricky. We cannot tell whether an empty card was removed
so we need to re-init empty cards periodically, to ensure their presence.
If the card was full of files and suddenly there are none. Use re-init to check
if it was removed.
- If we determined, the card got removed, let's tell Connect

Finally, we could have not noticed the card removal and the printer is telling
us about a SD insertion. Let's tell connect the card got removed and go to the
INITIALISING state

"""

import logging
from enum import Enum
from threading import Thread
from typing import Dict, Set

from blinker import Signal

from old_buddy.input_output.connect_api import ConnectAPI
from old_buddy.structures.model_classes import FileType, FileTree, \
    EmitEvents
from old_buddy.input_output.serial import Serial
from old_buddy.input_output.serial_queue.serial_queue import SerialQueue
from old_buddy.input_output.serial_queue.helpers import wait_for_instruction, \
    enqueue_matchable, enqueue_collecting
from old_buddy.settings import SD_CARD_LOG_LEVEL, \
    QUIT_INTERVAL, SD_INTERVAL
from old_buddy.structures.regular_expressions import INSERTED_REGEX, \
    SD_PRESENT_REGEX, BEGIN_FILES_REGEX, END_FILES_REGEX, FILE_PATH_REGEX
from old_buddy.threaded_updater import ThreadedUpdater
from old_buddy.util import run_slowly_die_fast

log = logging.getLogger(__name__)
log.setLevel(SD_CARD_LOG_LEVEL)


class CouldNotConstructTree(RuntimeError):
    ...


class InternalFileTree:

    @staticmethod
    def new_root_node():
        return InternalFileTree(file_type=FileType.DIR, path="sd_card")

    def __init__(self, file_type: FileType = None, path: str = None,
                 ro: bool = None, size: int = None,
                 m_date: int = None, m_time: int = None,
                 parent: 'InternalFileTree' = None):

        self.type = file_type
        self.path = path
        self.ro = ro
        self.size = size
        self.m_date = m_date
        self.m_time = m_time
        self.descendants_set: Set[InternalFileTree] = set()
        self.children_dict: Dict[str, InternalFileTree] = {}
        self._parent: InternalFileTree = parent

        self.full_path = self.get_full_path()

    def __hash__(self):
        return hash((self.type, self.ro, self.size, self.m_date, self.m_time))

    def __str__(self):
        output = self.get_full_path() + "\n"
        for child in self.children_dict.values():
            output += child.__str__()
        return output

    def __bool__(self):
        return bool(self.descendants_set)

    @property
    def parent(self):
        return self._parent

    @parent.setter
    def parent(self, parent: 'InternalFileTree'):
        self._parent = parent
        self.full_path = self.get_full_path()

    def add_child(self, child: 'InternalFileTree'):
        self.children_dict[child.path] = child
        if child.parent is None:
            child.parent = self
        log.debug(f"Child {child.path} added")
        return child

    def child_from_path(self, line: str):
        """
        Expected to be first called only on the root element,
        otherwise diffs break
        """
        log.debug(f"Parsing line {line}")
        clean_line = line.strip("/")
        parts = clean_line.split("/", 1)

        # Need to insert this deeper onto the tree, recurse
        if len(parts) == 2:
            path, rest = parts

            if path not in self.children_dict:
                child = InternalFileTree(file_type=FileType.DIR, path=path)
                self.add_child(child)

            log.debug(f"The file is in a directory {path}. Adding it inside")
            added_child = self.children_dict[path].child_from_path(rest)

        else:  # Insert to this level
            path, str_size = parts[0].split(" ")
            size = int(str_size)
            child = InternalFileTree(file_type=FileType.FILE, path=path,
                                     size=size)
            log.debug(f"Added a file {path} {size / 1000}kb to self")
            added_child = self.add_child(child)

        # if success (?) not expecting invalid strings, so I'm not checking
        self.descendants_set.add(added_child)
        return added_child

    def get_full_path(self):
        path = []
        current_node = self
        while current_node.parent is not None:
            # We do not need the root node's name, so this is sufficient
            path.append(current_node.path)
            current_node = current_node.parent

        return "/" + "/".join(reversed(path))

    def diff(self, other_tree: 'InternalFileTree'):
        removed_files = self.descendants_set.difference(
            other_tree.descendants_set)
        new_files = self.descendants_set.difference(other_tree.descendants_set)

        removed_paths = {file.full_path for file in removed_files}
        new_paths = {file.full_path for file in new_files}

        changed_file_paths = removed_paths.intersection(new_paths)

        for file in removed_files:
            if file.full_path in changed_file_paths:
                log.debug(f"File at {file.full_path} has been changed.")
            else:
                log.debug(f"File at {file.full_path} has been removed.")

        for file in new_files:
            if file.full_path not in changed_file_paths:
                log.debug(f"File at {file.full_path} has been created.")

    def to_api_file_tree(self):
        file_tree = FileTree()
        file_tree.type = self.type.name
        file_tree.path = self.path
        file_tree.ro = self.ro
        file_tree.size = self.size
        file_tree.m_date = self.m_date
        file_tree.m_time = self.m_time
        unconverted_children = list(self.children_dict.values())
        file_tree.children = [child.to_api_file_tree()
                              for child in unconverted_children]
        if not file_tree.children:
            file_tree.children = None
        return file_tree


class SDState(Enum):
    PRESENT = "PRESENT"
    INITIALISING = "INITIALISING"
    UNSURE = "UNSURE"
    ABSENT = "ABSENT"


class SDCard(ThreadedUpdater):
    thread_name = "sd_updater"
    update_interval = SD_INTERVAL

    def __init__(self, serial_queue: SerialQueue, serial: Serial):

        self.updated_signal = Signal()  # kwargs: tree: FileTree
        self.inserted_signal = Signal()  # kwargs: root: str, files: FileTree
        self.ejected_signal = Signal()  # kwargs: root: str

        self.serial = serial
        self.serial.register_output_handler(INSERTED_REGEX,
                                            lambda match: self.sd_inserted())
        self.serial_queue: SerialQueue = serial_queue

        self.expecting_insertion = False

        self.sd_state: SDState = SDState.UNSURE

        super().__init__()

    def _update(self):
        new_tree = self.construct_file_tree()

        unsure_states = {SDState.INITIALISING, SDState.UNSURE}

        # If we do not know the sd state and no files were found,
        # check the SD presence
        # If there were files and now there is nothing,
        # the SD was most likely ejected. So check for that
        if self.sd_state in unsure_states:
            if new_tree:
                self.sd_state_changed(SDState.PRESENT)
            else:
                self.decide_presence()
        if not new_tree and self.sd_state == SDState.PRESENT:
            self.decide_presence()
        if new_tree and self.sd_state == SDState.ABSENT:
            log.error("ERROR: Sanity check failed. SD is not present, "
                      "but we see files!")
        self.file_tree = new_tree

        api_file_tree = self.file_tree.to_api_file_tree()
        self.updated_signal.send(self, tree=api_file_tree,
                                 sd_state=self.sd_state)

    def construct_file_tree(self):
        tree = InternalFileTree(path="SD Card", file_type=FileType.MOUNT,
                                ro=True)

        if self.sd_state == SDState.ABSENT:
            return tree

        instruction = enqueue_collecting(self.serial_queue, "M20",
                                         begin_regex=BEGIN_FILES_REGEX,
                                         capture_regex=FILE_PATH_REGEX,
                                         end_regex=END_FILES_REGEX)
        wait_for_instruction(instruction, lambda: self.running)

        for match in instruction.captured_matches:
            tree.child_from_path(match.string.lower())

        log.debug(f"Constructed tree {tree}")
        return tree

    def sd_inserted(self):
        """
        If received while expecting it, stop expecting another one
        If received unexpectedly, this signalises someone physically
        inserting a card
        """
        if self.expecting_insertion:
            self.expecting_insertion = False
        else:
            self.sd_state_changed(SDState.INITIALISING)

    def sd_state_changed(self, new_state):
        log.debug(f"SD state changed from {self.sd_state} to "
                  f"{new_state}")

        if self.sd_state == SDState.INITIALISING and \
                new_state == SDState.PRESENT:
            log.debug("SD Card inserted")

            # When sending in info, it's in a different thread,
            # it can wait once the state becomes flagged as consistent
            # This is called in our own thread and would deadlock if we'd call
            # self.get_api_file_tree()
            # We know it's consistent because the card being confirmed present
            # is the last step before setting that event.
            # I had so much trouble detecting states I forgot what it will take
            # to send this
            files = self.file_tree.to_api_file_tree()
            self.inserted_signal.send(self, root="/", files=files)

        elif self.sd_state == SDState.PRESENT and \
                new_state in {SDState.ABSENT, SDState.INITIALISING}:
            log.debug("SD Card removed")
            self.ejected_signal.send(self, root="/")

        self.sd_state = new_state

    def decide_presence(self):
        """
        Calling this can be disruptive to the user experience,
        the card will reload. If there is nothing on the SD card or
        if we suspect there is no SD card, calling this should be fine
        """
        self.expecting_insertion = True
        instruction = enqueue_matchable(self.serial_queue, "M21")
        wait_for_instruction(instruction, lambda: self.running)
        self.expecting_insertion = False

        if not instruction.is_confirmed():
            log.debug("Failed determining the SD presence.")
        else:
            match = instruction.match(SD_PRESENT_REGEX)
            if match.groups()[0] is not None:
                if self.sd_state != SDState.PRESENT:
                    self.sd_state_changed(SDState.PRESENT)
            else:
                self.sd_state_changed(SDState.ABSENT)

