import tempfile
import unittest
import zipfile
from pathlib import Path

from isolated import IsolatedHome
from finances import importer
from finances.db import Database


def make_xlsx(path, rows):
    """A minimal .xlsx with inline strings, laid out like a Santander export (intro lines first)."""
    def cell(ref, v):
        return f'<c r="{ref}" t="inlineStr"><is><t>{v}</t></is></c>'
    body = ""
    for i, r in enumerate(rows, 1):
        body += f'<row r="{i}">' + "".join(cell(f"{chr(65 + j)}{i}", v) for j, v in enumerate(r) if v != "") + "</row>"
    xml = ('<?xml version="1.0"?><worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
           f"<sheetData>{body}</sheetData></worksheet>")
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("xl/worksheets/sheet1.xml", xml)


ROWS = [
    ["", "", "Cuenta", "Fecha"], ["", "", "ES00 0000", "16/11/2026 | 22:06:10"],
    ["Movimientos"],
    ["Fecha operación", "Fecha valor", "Concepto", "Importe", "Saldo", "Divisa"],
    ["16/11/2026", "16/11/2026", "RECIBO LUZ", "-76,29€", "1.385,15€", "EUR"],
    ["15/11/2026", "15/11/2026", "PAGO MOVIL EN FRUTAS", "-10,01€", "1.461,44€", "EUR"],
    ["14/11/2026", "14/11/2026", "NOMINA EMPRESA", "1.274,19€", "1.471,45€", "EUR"],
    ["01/06/2026", "01/06/2026", "RECIBO LUZ", "-70,00€", "500,00€", "EUR"],
]


class ImportTests(IsolatedHome):
    def setUp(self):
        super().setUp()
        self.db = Database("pw")
        self.db.upsert_account(dict(id="san", provider="santander", name="Santander", kind="cash",
                                    currency="EUR", balance=1385.15, updated_at="2026-11-16"))
        # the bank sync already delivered the luz receipt, dated a day later than the export says
        self.db.upsert_transaction(dict(id="san-1", account_id="san", date="2026-11-17", amount=-76.29,
                                        description="PEPE ENERGY – recibo", category="uncategorized"))
        self.file = Path(tempfile.mkdtemp()) / "Santander_export.xlsx"
        make_xlsx(self.file, ROWS)

    def test_parsing_spanish_numbers_and_dates(self):
        rows = importer.parse_export(importer.read_xlsx(self.file))
        self.assertEqual(len(rows), 4)
        self.assertEqual((rows[2]["date"], rows[2]["amount"]), ("2026-11-14", 1274.19))
        self.assertEqual(importer.parse_date("46342"), "2026-11-16")

    def test_import_skips_what_the_bank_already_gave_and_is_idempotent(self):
        out = importer.import_file(self.db, self.file)
        self.assertEqual((out["read"], out["added"], out["already_there"]), (4, 3, 1))
        again = importer.import_file(self.db, self.file)
        self.assertEqual((again["added"], again["already_there"]), (0, 4))
        n = self.db.conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
        self.assertEqual(n, 4)

    def test_later_bank_sync_of_an_imported_entry_is_flagged_not_doubled(self):
        importer.import_file(self.db, self.file)
        self.db.upsert_transaction(dict(id="san-2", account_id="san", date="2026-11-15", amount=-10.01,
                                        description="FRUTAS ALADINO", category="uncategorized"))
        self.assertEqual(importer.reconcile(self.db), 1)
        dup = self.db.conn.execute("SELECT COUNT(*) FROM transactions WHERE duplicate=1").fetchone()[0]
        self.assertEqual(dup, 1)

    def test_clear_errors(self):
        bad = self.file.with_name("x.xlsx")
        make_xlsx(bad, [["nothing", "useful"]])
        with self.assertRaises(ValueError):
            importer.import_file(self.db, bad)
        with self.assertRaises(ValueError):
            importer.import_file(self.db, self.file.with_name("missing.xlsx"))
        self.db.conn.execute("DELETE FROM accounts")
        with self.assertRaises(ValueError):
            importer.import_file(self.db, self.file)


if __name__ == "__main__":
    unittest.main()
