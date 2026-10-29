import json


def to_json_line(row):
    return json.dumps(list(row))
