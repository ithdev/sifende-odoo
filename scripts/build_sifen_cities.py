"""Genera data/l10n_py.sifende.city.csv desde insert-ref-geografica.sql de Sifende.

Uso: python3 scripts/build_sifen_cities.py <ruta a insert-ref-geografica.sql>
"""

import csv
import re
import sys
from pathlib import Path

OUTPUT = (
    Path(__file__).parent.parent
    / "l10n_py_edi_sifende"
    / "data"
    / "l10n_py.sifende.city.csv"
)


def rows(sql, table, pattern):
    block = re.search(rf"INSERT INTO {table} \([^)]*\) VALUES\n(.*?);\n", sql, re.S)
    return [
        match.groups()
        for line in block.group(1).splitlines()
        if (match := re.match(pattern, line.strip().rstrip(",")))
    ]


def main(source):
    sql = Path(source).read_text(encoding="utf-8")
    departments = {
        int(pk): (int(code), name)
        for pk, code, name in rows(sql, "departamento", r"\((\d+), (\d+), '(.*)'\)$")
    }
    districts = {
        int(pk): (departments[int(dep)], int(code), name.replace("''", "'"))
        for pk, dep, code, name in rows(
            sql, "distrito", r"\((\d+), (\d+), (\d+), '(.*)'\)$"
        )
    }
    cities = rows(sql, "ciudad", r"\((\d+), (\d+), (\d+), '(.*)'\)$")
    OUTPUT.parent.mkdir(exist_ok=True)
    with OUTPUT.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(
            [
                "id",
                "name",
                "code",
                "district_code",
                "district_name",
                "department_code",
                "department_name",
            ]
        )
        for _pk, district, code, name in cities:
            (dep_code, dep_name), dist_code, dist_name = districts[int(district)]
            writer.writerow(
                [
                    f"city_{code}",
                    name.replace("''", "'"),
                    code,
                    dist_code,
                    dist_name,
                    dep_code,
                    dep_name,
                ]
            )
    sys.stdout.write(f"{len(cities)} ciudades en {OUTPUT}\n")


if __name__ == "__main__":
    main(sys.argv[1])
