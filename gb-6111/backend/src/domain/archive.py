import csv
from dataclasses import dataclass, replace
from hashlib import sha256
from io import StringIO

from .relations import RELATIONS, select_sql


@dataclass(frozen=True)
class Segment:
    relation: str
    data: bytes
    digest: str

    @classmethod
    def capture(cls, relation, data):
        return cls(relation, data, sha256(data).hexdigest())

    def actual_digest(self):
        return sha256(self.data).hexdigest()

    def verified(self):
        return self.actual_digest() == self.digest

    def record_count(self):
        text_data = self.data.decode("utf-8")
        if not text_data:
            return 0
        return sum(1 for _ in csv.reader(StringIO(text_data)))


def export_selection(db, source, requested):
    segments = []
    with db.connection.driver_connection.cursor() as cursor:
        for rel in RELATIONS:
            select = select_sql(source, rel)
            with cursor.copy(f"COPY ({select}) TO STDOUT WITH (FORMAT CSV)", (requested,)) as copy:
                data = b"".join(bytes(chunk) for chunk in copy)
            segments.append(Segment.capture(rel.key, data))
    return segments


def damage_segment(segments, damaged_relation):
    return [
        replace(item, data=item.data + b"transport-corruption\n")
        if item.relation == damaged_relation else item
        for item in segments
    ]


def apply_archive(db, target, segments):
    with db.connection.driver_connection.cursor() as cursor:
        for rel, segment in zip(RELATIONS, segments, strict=True):
            columns = ", ".join(rel.columns)
            with cursor.copy(f"COPY {target[rel.key]} ({columns}) FROM STDIN WITH (FORMAT CSV)") as copy:
                copy.write(segment.data)


def reexport(db, target, relation):
    columns = ", ".join(relation.columns)
    with db.connection.driver_connection.cursor() as cursor:
        with cursor.copy(
            f'COPY (SELECT {columns} FROM {target[relation.key]} '
            f'ORDER BY {relation.columns[0]} COLLATE "C") TO STDOUT WITH (FORMAT CSV)'
        ) as copy:
            return Segment.capture(relation.key, b"".join(bytes(chunk) for chunk in copy))
