"""What the saved log file hides: shapes of secrets, the vault's values, the person's chosen personal data and words."""
import tempfile
import unittest
from pathlib import Path

from harness.vault import Vault
from serve.util.redact import HIDDEN, ExportMask, clean_words, luhn


class Redact(unittest.TestCase):
    def test_the_common_secret_shapes_are_hidden(self):
        mask = ExportMask()
        for text in ("-----BEGIN PRIVATE KEY-----\nMIIEvQIBADANBgkqhkiG9w0BAQEFAASC\n-----END PRIVATE KEY-----",
                     "AKIAABCDEFGHIJKLMNOP", "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.abcdefghij0123456789", "xoxb-1234567890-abcdefghij",
                     "Authorization: Bearer abcdefghijklmnop0123", '{"api_key": "abcdef123456"}', "password=correct-horse-battery", "https://me:s3cretpass@host.example/x",
                     "DB_PASSWORD: 'hunter2hunter2'", "token = abcdef0123456789"):
            out = mask.text(text)
            self.assertIn(HIDDEN, out, text)
            for fragment in ("MIIEvQIBADANBgkq", "AKIAABCDEFGHIJKLMNOP", "abcdefghij0123456789", "1234567890-abcdef", "abcdefghijklmnop0123", "abcdef123456",
                             "correct-horse-battery", "s3cretpass", "hunter2hunter2", "abcdef0123456789"):
                self.assertNotIn(fragment, out, (text, fragment))

    def test_plain_text_and_numbers_that_are_not_cards_are_left_alone(self):
        mask = ExportMask()
        text = "Write the function that returns the token count; call it with 1234 5678 9012 3456 and the ID 20261027."
        self.assertEqual(mask.text(text), text)

    def test_a_card_number_is_found_by_its_check_digit_however_it_is_spaced(self):
        mask = ExportMask()
        for number in ("4242424242424242", "4242 4242 4242 4242", "4242-4242-4242-4242", "378282246310005"):
            self.assertEqual(mask.text(f"pay {number} now"), f"pay {HIDDEN} now", number)
        self.assertTrue(luhn("4242424242424242") and not luhn("4242424242424241"))

    def test_the_values_in_the_vault_and_the_servers_key_are_hidden_by_value(self):
        with tempfile.TemporaryDirectory() as d:
            vault = Vault(Path(d))
            vault.put("me", kind="identity", label="Me", fields={"full_name": "Mario Rossi", "city": "Cortina"})
            mask = ExportMask(known=["srv-key-0001"], vault_mask=vault.mask)
            out = mask.text("Mario Rossi lives in Cortina; key srv-key-0001")
            self.assertNotIn("Mario Rossi", out)
            self.assertNotIn("Cortina", out)
            self.assertNotIn("srv-key-0001", out)

    def test_personal_data_is_hidden_only_when_asked(self):
        text = "write to mario@example.com or +39 333 1234567, IBAN IT60 X054 2811 1010 0000 0123 456, tax RSSMRA80A01H501U"
        self.assertEqual(ExportMask("secrets").text(text), text)
        out = ExportMask("personal").text(text)
        for gone in ("mario@example.com", "333 1234567", "0123 456", "RSSMRA80A01H501U"):
            self.assertNotIn(gone, out, gone)

    def test_listed_words_are_hidden_in_any_case_and_the_list_is_bounded(self):
        mask = ExportMask("secrets", ["Mario Rossi", "Cervino"])
        self.assertEqual(mask.text("MARIO ROSSI booked the cervino room"), f"{HIDDEN} booked the {HIDDEN} room")
        self.assertEqual(len(clean_words(",".join(f"word{i}" for i in range(50)))), 20)
        self.assertEqual(clean_words("a, ,  ok word ,x" + "y" * 80), ["ok word"])

    def test_structured_data_is_hidden_by_field_name_too(self):
        mask = ExportMask()
        out = mask.value({"headers": {"authorization": "Bearer short", "x": "ok"}, "list": [{"api_key": "k1"}], "n": 3})
        self.assertEqual(out, {"headers": {"authorization": HIDDEN, "x": "ok"}, "list": [{"api_key": HIDDEN}], "n": 3})

    def test_an_unknown_level_is_an_error(self):
        with self.assertRaises(ValueError):
            ExportMask("everything")


if __name__ == "__main__":
    unittest.main()
