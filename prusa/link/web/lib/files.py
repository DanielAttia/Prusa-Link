"""Check and modify an input dictionary using recursion"""

from datetime import datetime
from os.path import join

from prusa.connect.printer.const import GCODE_EXTENSIONS


def files_to_api(node, origin='local', path='/'):
    """Convert Prusa SDK Files tree for API.

    >>> files = {'type': 'DIR', 'name': '/', 'ro': True, 'children':[
    ...     {'type': 'DIR', 'name': 'SD Card', 'children':[
    ...         {'type': 'DIR', 'name': 'Examples', 'children':[
    ...             {'type': 'FILE', 'name': '1.gcode'},
    ...             {'type': 'FILE', 'name': 'b.gco'}]}]},
    ...     {'type': 'DIR', 'name': 'Prusa Link gcodes', 'children':[
    ...         {'type': 'DIR', 'name': 'Examples', 'children':[
    ...             {'type': 'FILE', 'name': '1.gcode'},
    ...             {'type': 'FILE', 'name': 'b.gco'}]}]},
    ...     {'type': 'FILE', 'name': 'preview.png'}
    ... ]}
    >>> api_files = files_to_api(files)
    >>> # /
    >>> api_files['type']
    'folder'
    >>> # /SD Card
    >>> api_files['children'][0]['type']
    'folder'
    >>> # /SD Card/Examples
    >>> api_files['children'][0]['children'][0]['type']
    'folder'
    >>> api_files['children'][0]['children'][0]['path']
    '/SD Card/Examples'
    >>> #'/SD Card/Examples/1.gcode'
    >>> api_files['children'][0]['children'][0]['children'][0]['type']
    'machinecode'
    >>> api_files['children'][0]['children'][0]['children'][0]['origin']
    'sdcard'
    >>> # /Prusa Link gcodes/Examples
    >>> api_files['children'][1]['children'][0]['type']
    'folder'
    >>> # /Prusa Link gcodes/Examples/1.gcode
    >>> api_files['children'][1]['children'][0]['children'][0]['type']
    'machinecode'
    >>> api_files['children'][1]['children'][0]['children'][0]['origin']
    'local'
    >>> len(api_files['children'])
    2
    """
    name = node['name']
    path = join(path, name)

    result = {'name': name, 'path': path, 'display': name}

    if "m_time" in node:
        result["date"] = int(datetime(*node['m_time']).timestamp())

    if 'size' in node:
        result['size'] = node['size']

    if node['type'] == 'DIR':
        if name == 'SD Card':
            origin = 'sdcard'

        result['type'] = 'folder'
        result['typePath'] = ['folder']
        result['origin'] = origin
        result['refs'] = {"resource": None}

        children = list(
            files_to_api(child, origin, path)
            for child in node.get("children", []))
        result['children'] = list(child for child in children if child)

    elif name.endswith(GCODE_EXTENSIONS):
        result['origin'] = origin
        result['type'] = 'machinecode'
        result['typePath'] = ['machinecode', 'gcode']
        result['date'] = None
        result['hash'] = None
        result['refs'] = {
            'resource': None,
            'download': None,
            'thumbnailSmall': None,
            'thumbnailBig': None
        }
        result['gcodeAnalysis'] = {
            'estimatedPrintTime': None,
            'material': None,
            'layerHeight': None
        }

    else:
        return {}  # not folder or allowed extension

    return result