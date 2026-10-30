"""cells.json and declared.tsv: what the committed configuration must hold."""

import json
import tempfile
import unittest
from pathlib import Path

from compatlib import config


class Committed(unittest.TestCase):
    def setUp(self):
        self.cells = config.load_cells()
        self.declared = config.load_declarations(self.cells)

    def test_every_cell_is_declared_down_to_table_type_and_operation(self):
        # Both forms, before either runs: a form without its declarations cannot run its cells.
        for form in config.FORMS:
            for cell in self.cells["cells"]:
                for table in config.TABLE_TYPES:
                    for op in config.OPERATIONS:
                        self.assertIn((form, cell["id"], table, op), self.declared)

    def test_the_writer_cells_are_the_designs(self):
        writers = {(c["engine"], c["engineVersion"][:4].rstrip("."), c["paimon"])
                   for c in self.cells["cells"] if c["kind"] in config.WRITERS}
        self.assertEqual(writers, {("Flink", "1.20", "2.0.0"), ("Flink", "1.20", "2.1.0"), ("Flink", "2.2", "2.1.0"),
                                   ("Spark", "3.5", "2.0.0"), ("Spark", "3.5", "2.1.0"), ("Spark", "4.1", "2.1.0")})

    def test_writers_are_declared_supported_everywhere(self):
        for (form, cell, table, op), (value, _) in self.declared.items():
            if config.cell_by_id(self.cells, cell)["kind"] in config.WRITERS:
                self.assertEqual(value, "supported", (cell, table, op))

    def test_a_writer_runs_its_own_paimon_version(self):
        for cell in self.cells["cells"]:
            if cell["kind"] in config.WRITERS:
                for jar in cell["jars"]:
                    self.assertTrue(jar.endswith(f"-{cell['paimon']}.jar"), (cell["id"], jar))


class Refused(unittest.TestCase):
    def write(self, data):
        f = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        json.dump(data, f)
        f.close()
        self.addCleanup(Path(f.name).unlink)
        return f.name

    def cells(self, **change):
        data = json.loads(config.CELLS_FILE.read_text())
        data.update(change)
        return data

    def test_an_image_by_tag_only(self):
        data = self.cells()
        data["images"]["flink-1.20"] = "docker.io/library/flink:1.20.5"
        with self.assertRaisesRegex(config.ConfigError, "image flink-1.20"):
            config.load_cells(self.write(data))

    def test_a_jar_without_its_checksum(self):
        data = self.cells()
        data["jars"]["paimon-s3-2.1.0.jar"]["sha256"] = ""
        with self.assertRaisesRegex(config.ConfigError, "paimon-s3-2.1.0.jar: no SHA-256"):
            config.load_cells(self.write(data))

    def test_a_declaration_missing(self):
        lines = config.DECLARED_FILE.read_text().splitlines()
        f = tempfile.NamedTemporaryFile("w", suffix=".tsv", delete=False)
        f.write("\n".join(line for line in lines if not line.startswith("eks\tdoris-4.1.5\tappend\tread\t")) + "\n")
        f.close()
        self.addCleanup(Path(f.name).unlink)
        with self.assertRaisesRegex(config.ConfigError, "nothing declared for eks doris-4.1.5 append read"):
            config.load_declarations(config.load_cells(), f.name)

    def test_a_limitation_without_its_reason(self):
        lines = config.DECLARED_FILE.read_text().splitlines()
        f = tempfile.NamedTemporaryFile("w", suffix=".tsv", delete=False)
        f.write("\n".join(line.replace("\tsupported", "\tlimitation", 1)
                          if line.startswith("eks\tflink-1.20-paimon-2.0.0\tpk\tddl\t") else line
                          for line in lines) + "\n")
        f.close()
        self.addCleanup(Path(f.name).unlink)
        with self.assertRaisesRegex(config.ConfigError, "a limitation needs its reason"):
            config.load_declarations(config.load_cells(), f.name)


if __name__ == "__main__":
    unittest.main()
