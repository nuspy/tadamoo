"""The base file the quality gate writes for the engine: header, tokens and the space of the rows, as the reader expects.

    python3 -m unittest tools.test_ppl_gate_base
"""
import json
import os
import struct
import tempfile
import unittest

import ppl_gate
from model_file import ModelWriter
from tokenizer import bytes_to_unicode


class BaseFile(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.dir.name, "base.kld")

    def tearDown(self):
        self.dir.cleanup()

    def test_the_header_the_tokens_and_the_reserved_rows(self):
        n_ctx, n_vocab = 8, 7
        chunks = [[(k * 3 + i) % n_vocab for i in range(n_ctx)] for k in range(2)]
        size = ppl_gate.write_base_file(self.path, chunks, n_ctx, n_vocab)
        self.assertEqual(os.path.getsize(self.path), size)
        # 20 header bytes, 2 chunks of 8 tokens, 2 chunks of 3 rows, each row 4 + 8 u16 (an odd vocabulary is padded)
        self.assertEqual(size, 20 + 2 * 8 * 4 + 2 * 3 * (4 + 8) * 2)
        n_ctx_r, n_vocab_r, n_chunk_r, chunks_r = ppl_gate.read_base(self.path)
        self.assertEqual((n_ctx_r, n_vocab_r, n_chunk_r), (n_ctx, n_vocab, 2))
        self.assertEqual([list(c) for c in chunks_r], chunks)

    def test_the_row_size_of_the_real_vocabulary(self):
        self.assertEqual(ppl_gate.row_bytes(248320), 2 * (248320 + 4))
        self.assertEqual(ppl_gate.row_bytes(248321), 2 * (248322 + 4))

    def test_a_base_is_never_written_over_another(self):
        ppl_gate.write_base_file(self.path, [[0] * 4], 4, 5)
        with self.assertRaises(FileExistsError):
            ppl_gate.write_base_file(self.path, [[1] * 4], 4, 5)

    def test_a_chunk_of_the_wrong_length_is_refused(self):
        with self.assertRaises(ValueError):
            ppl_gate.write_base_file(self.path, [[0] * 3], 4, 5)
        self.assertFalse(os.path.exists(self.path))


class Tokenizing(unittest.TestCase):
    """The text is cut with the tokenizer sections of the real model file, which are named `tokenizer/...`."""

    def test_a_pack_with_the_real_section_names_gives_its_ids_and_its_vocabulary_size(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "m.basalt")
            chars = [bytes_to_unicode()[b] for b in range(256)]
            w = ModelWriter(path)
            w.add_bytes("tokenizer/vocab.json", json.dumps({c: i for i, c in enumerate(chars + ["ab"])}).encode())
            w.add_bytes("tokenizer/merges.txt", b"a b\r\n\xc4\xa0 a")        # a file written on Windows
            w.add_bytes("tokenizer/token_type.json", json.dumps([1] * 257).encode())
            w.add_bytes("tokenizer/tokenizer.json", json.dumps({"pre": "qwen35"}).encode())
            w.add_bytes("meta", json.dumps({"tensors": {"output.weight": {"shape": [2560, 248320]}}}).encode())
            w.close()
            ids, n_vocab = ppl_gate.tokenize_text(path, "ab")
        self.assertEqual(ids, [256])                       # "a" "b" merged into "ab"
        self.assertEqual(n_vocab, 248320)


class PairedError(unittest.TestCase):
    def test_the_error_is_taken_over_chunk_means(self):
        # two chunks whose rows differ a lot inside but whose means are 1 and 3: the error is sqrt(var of the means / 2)
        se = ppl_gate.paired_se({0: [0.0, 2.0], 1: [2.0, 4.0]})
        self.assertAlmostEqual(se, 1.0)
        self.assertTrue(ppl_gate.paired_se({0: [1.0]}) != ppl_gate.paired_se({0: [1.0]}))   # nan with one chunk


if __name__ == "__main__":
    unittest.main()
