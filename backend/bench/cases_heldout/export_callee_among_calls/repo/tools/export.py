from tools.csvfmt import to_csv_line
from tools.logs import log_info


def export_rows(rows, path):
    lines = [to_csv_line(row) for row in rows]
    with open(path, "w") as handle:
        handle.write("\n".join(lines))
    log_info(f"exported {len(lines)} rows")
    return len(lines)
