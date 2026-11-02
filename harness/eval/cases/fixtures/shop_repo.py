"""E1 fixture: a tiny repository and eight multi-part goals on it, each with the checks that decide it and a reference solution.

The checks use the evaluation's own check types (`harness/eval/tasks.py`) and run the repository's own tests and greps. The solution of a
goal is the files it changes, whole; it exists so a test can show that the checks pass on a good result and fail on the untouched repository.
"""
from __future__ import annotations

from typing import Any, Dict, List

TEST_CMD = "{python} -m unittest discover -q"
VERIFY_CMD = "python -m unittest discover -q"

REPO: Dict[str, str] = {
    "shop/__init__.py": '__version__ = "0.1.0"\n',
    "shop/pricing.py": (
        "def apply_discount(price, percent):\n    return price - price * percent / 100\n\n\n"
        "def with_tax(price, rate=0.22):\n    return round(price * (1 + rate), 2)\n"),
    "shop/cart.py": (
        "from shop.pricing import apply_discount, with_tax\n\n\nclass Cart:\n    def __init__(self):\n        self.items = []\n\n"
        "    def add(self, name, price, qty=1):\n        self.items.append((name, price, qty))\n\n"
        "    def subtotal(self):\n        return sum(price * qty for _, price, qty in self.items)\n\n"
        "    def total(self, discount=0):\n        return with_tax(apply_discount(self.subtotal(), discount))\n"),
    "shop/utils.py": (
        'def format_money(amount):\n    return f"EUR {amount:.2f}"\n\n\n'
        'def slugify(text):\n    return "-".join(text.lower().split())\n'),
    "tests/__init__.py": "",
    "tests/test_pricing.py": (
        "import unittest\n\nfrom shop.pricing import apply_discount, with_tax\n\n\nclass PricingTest(unittest.TestCase):\n"
        "    def test_discount(self):\n        self.assertEqual(apply_discount(200, 10), 180)\n\n"
        "    def test_tax(self):\n        self.assertEqual(with_tax(100), 122.0)\n"),
    "tests/test_cart.py": (
        "import unittest\n\nfrom shop.cart import Cart\n\n\nclass CartTest(unittest.TestCase):\n    def test_total(self):\n"
        "        cart = Cart()\n        cart.add(\"pen\", 2.0, 3)\n        cart.add(\"book\", 10.0)\n"
        "        self.assertEqual(cart.subtotal(), 16.0)\n        self.assertEqual(cart.total(), 19.52)\n"),
    "tests/test_utils.py": (
        "import unittest\n\nfrom shop.utils import format_money, slugify\n\n\nclass UtilsTest(unittest.TestCase):\n"
        "    def test_money(self):\n        self.assertEqual(format_money(3), \"EUR 3.00\")\n\n"
        "    def test_slug(self):\n        self.assertEqual(slugify(\"Red Pen\"), \"red-pen\")\n"),
    "docs/api.md": (
        "# shop API\n\n- `shop.pricing.apply_discount(price, percent)`: the price after a percentage discount.\n"
        "- `shop.pricing.with_tax(price, rate=0.22)`: the price with tax, rounded to cents.\n"
        "- `shop.cart.Cart`: items, `subtotal()` and `total(discount=0)`.\n- `shop.utils.format_money(amount)`: for example `EUR 12.50`.\n"
        "- `shop.utils.slugify(text)`: lower case words joined by `-`.\n"),
    "README.md": "# shop\n\nA tiny shopping cart.\n\n## Features\n\n- a cart with discount and tax\n- money formatting\n",
}

UTILS_OK = 'def format_money(amount):\n    return f"EUR {amount:.2f}"\n\n\ndef slugify(text):\n    return "-".join(text.lower().split())\n'


def _cmd(code: str) -> Dict[str, Any]:
    return {"type": "command", "cmd": '{python} -c "' + code + '"'}


SUITE = {"type": "command", "cmd": TEST_CMD}


def _with(path: str, old: str, new: str) -> str:
    text = REPO[path]
    assert old in text, (path, old)
    return text.replace(old, new, 1)


GOALS: List[Dict[str, Any]] = [
    {
        "id": "g1_clamp",
        "text": ("I want a clamp helper in the shop package.\n1. Add `clamp(value, low, high)` to shop/utils.py: it returns value limited to the range low..high.\n"
                 "2. Add tests for it to tests/test_utils.py.\n3. Add a line for it to docs/api.md.\nThe whole test suite must pass at the end."),
        "files": {},
        "checks": [_cmd("from shop.utils import clamp; assert clamp(5,0,3)==3 and clamp(-1,0,3)==0 and clamp(2,0,3)==2"),
                   {"type": "file_contains", "path": "tests/test_utils.py", "text": "clamp"},
                   {"type": "file_contains", "path": "docs/api.md", "text": "clamp"}, SUITE],
        "solution": {
            "shop/utils.py": REPO["shop/utils.py"] + "\n\ndef clamp(value, low, high):\n    return max(low, min(high, value))\n",
            "tests/test_utils.py": REPO["tests/test_utils.py"].replace("from shop.utils import format_money, slugify", "from shop.utils import clamp, format_money, slugify") +
            "\n    def test_clamp(self):\n        self.assertEqual(clamp(5, 0, 3), 3)\n        self.assertEqual(clamp(-1, 0, 3), 0)\n",
            "docs/api.md": REPO["docs/api.md"] + "- `shop.utils.clamp(value, low, high)`: value limited to the range.\n"},
    },
    {
        "id": "g2_rename",
        "text": ("Rename the function `apply_discount` to `discount_price` everywhere it appears.\n- the code in shop/\n- the tests\n- the docs\n"
                 "Nothing else changes in behaviour, and the tests must still pass."),
        "files": {},
        "checks": [{"type": "file_not_contains", "path": p, "text": "apply_discount"} for p in ("shop/pricing.py", "shop/cart.py", "tests/test_pricing.py", "docs/api.md")] +
                  [_cmd("from shop.pricing import discount_price; assert discount_price(200, 10) == 180"), SUITE],
        "solution": {p: REPO[p].replace("apply_discount", "discount_price") for p in ("shop/pricing.py", "shop/cart.py", "tests/test_pricing.py", "docs/api.md")},
    },
    {
        "id": "g3_two_failing_tests",
        "text": ("Two tests in tests/test_utils.py fail.\n1. Find out why and fix the code, not the tests.\n"
                 "2. Make the whole suite pass.\n3. Add a line to the Features list in README.md saying that money is shown with two decimals."),
        "files": {
            "shop/utils.py": 'def format_money(amount):\n    return f"EUR {amount:.1f}"\n\n\ndef slugify(text):\n    return "-".join(text.lower().split(" "))\n',
            "tests/test_utils.py": REPO["tests/test_utils.py"] + "\n    def test_slug_spaces(self):\n        self.assertEqual(slugify(\"Red  Pen\"), \"red-pen\")\n"},
        "checks": [{"type": "file_unchanged", "path": "tests/test_utils.py"}, {"type": "file_contains", "path": "README.md", "text": "two decimals"}, SUITE],
        "solution": {"shop/utils.py": UTILS_OK, "README.md": REPO["README.md"] + "- money is shown with two decimals\n"},
    },
    {
        "id": "g4_cart_remove_count",
        "text": ("Extend the cart.\n1. `Cart.remove(name)` removes the item with that name and returns True; it returns False when there is none.\n"
                 "2. `Cart.count()` returns the total quantity of all items.\n3. Tests for both in tests/test_cart.py.\n"
                 "4. A line in README.md under Features that mentions removing items."),
        "files": {},
        "checks": [_cmd("from shop.cart import Cart; c=Cart(); c.add('a',1,2); c.add('b',2); assert c.count()==3; assert c.remove('a') is True; "
                        "assert c.remove('zz') is False; assert c.count()==1"),
                   {"type": "file_contains", "path": "tests/test_cart.py", "text": "remove"}, {"type": "file_contains", "path": "README.md", "text": "remov"}, SUITE],
        "solution": {
            "shop/cart.py": REPO["shop/cart.py"] + (
                "\n    def remove(self, name):\n        kept = [i for i in self.items if i[0] != name]\n        found = len(kept) != len(self.items)\n"
                "        self.items = kept\n        return found\n\n    def count(self):\n        return sum(qty for _, _, qty in self.items)\n"),
            "tests/test_cart.py": REPO["tests/test_cart.py"] + (
                "\n    def test_remove_and_count(self):\n        cart = Cart()\n        cart.add(\"a\", 1, 2)\n        self.assertEqual(cart.count(), 2)\n"
                "        self.assertTrue(cart.remove(\"a\"))\n        self.assertFalse(cart.remove(\"a\"))\n"),
            "README.md": REPO["README.md"] + "- removing items from the cart\n"},
    },
    {
        "id": "g5_thousands",
        "text": ("Money should be shown with thousands separators: 1234.5 becomes `EUR 1,234.50`.\n1. Change `format_money`.\n"
                 "2. Add that case to the money test.\n3. Update the example in docs/api.md to show it."),
        "files": {},
        "checks": [_cmd("from shop.utils import format_money; assert format_money(1234.5) == 'EUR 1,234.50' and format_money(3) == 'EUR 3.00'"),
                   {"type": "file_contains", "path": "tests/test_utils.py", "text": "1,234.50"}, {"type": "file_contains", "path": "docs/api.md", "text": "1,234.50"}, SUITE],
        "solution": {
            "shop/utils.py": UTILS_OK.replace('{amount:.2f}', '{amount:,.2f}'),
            "tests/test_utils.py": REPO["tests/test_utils.py"].replace('"EUR 3.00")', '"EUR 3.00")\n        self.assertEqual(format_money(1234.5), "EUR 1,234.50")'),
            "docs/api.md": REPO["docs/api.md"].replace("`EUR 12.50`", "`EUR 1,234.50`")},
    },
    {
        "id": "g6_move_text_helpers",
        "text": ("Move the text helpers out of shop/utils.py.\n1. Put `slugify` and `format_money` in a new module shop/text.py.\n"
                 "2. Code that imports them from shop.utils must keep working.\n3. Add tests/test_text.py with a test for each of them.\nThe suite must pass."),
        "files": {},
        "checks": [{"type": "file_contains", "path": "shop/text.py", "text": "def slugify"}, {"type": "file_contains", "path": "shop/text.py", "text": "def format_money"},
                   {"type": "file_not_contains", "path": "shop/utils.py", "text": "def slugify"},
                   _cmd("from shop.utils import slugify, format_money; from shop.text import slugify as s; assert s('A B') == 'a-b'"),
                   {"type": "file_contains", "path": "tests/test_text.py", "text": "slugify"}, SUITE],
        "solution": {
            "shop/text.py": UTILS_OK,
            "shop/utils.py": "from shop.text import format_money, slugify  # noqa: F401\n",
            "tests/test_text.py": ("import unittest\n\nfrom shop.text import format_money, slugify\n\n\nclass TextTest(unittest.TestCase):\n"
                                   "    def test_slugify(self):\n        self.assertEqual(slugify(\"A B\"), \"a-b\")\n\n"
                                   "    def test_money(self):\n        self.assertEqual(format_money(1), \"EUR 1.00\")\n")},
    },
    {
        "id": "g7_validation",
        "text": ("Make `Cart.add` strict.\n1. A negative price raises ValueError.\n2. A quantity below 1 raises ValueError.\n"
                 "3. Tests for both cases in tests/test_cart.py.\n4. Say so in docs/api.md (the word Raises and the error name)."),
        "files": {},
        "checks": [_cmd("import unittest; from shop.cart import Cart; t = unittest.TestCase(); "
                        "[t.assertRaises(ValueError, Cart().add, *a) for a in (('x', -1, 1), ('x', 1, 0))]; Cart().add('ok', 1, 1)"),
                   {"type": "file_contains", "path": "tests/test_cart.py", "text": "ValueError"}, {"type": "file_contains", "path": "docs/api.md", "text": "Raises"}, SUITE],
        "solution": {
            "shop/cart.py": REPO["shop/cart.py"].replace(
                "        self.items.append((name, price, qty))",
                "        if price < 0 or qty < 1:\n            raise ValueError(\"price must not be negative and qty must be at least 1\")\n        self.items.append((name, price, qty))"),
            "tests/test_cart.py": REPO["tests/test_cart.py"] + (
                "\n    def test_add_is_strict(self):\n        cart = Cart()\n        with self.assertRaises(ValueError):\n            cart.add(\"x\", -1)\n"
                "        with self.assertRaises(ValueError):\n            cart.add(\"x\", 1, 0)\n"),
            "docs/api.md": REPO["docs/api.md"] + "- `Cart.add(name, price, qty=1)` Raises ValueError for a negative price or a quantity below 1.\n"},
    },
    {
        "id": "g8_discount_cap_release",
        "text": ("Prepare release 0.2.0.\n1. `apply_discount` must treat a percent above 100 as 100 and raise ValueError for a negative percent.\n"
                 "2. Tests for both in tests/test_pricing.py.\n3. Set the version in shop/__init__.py to 0.2.0.\n"
                 "4. Create CHANGELOG.md with a heading for 0.2.0 and one bullet for the discount change."),
        "files": {},
        "checks": [_cmd("from shop.pricing import apply_discount as d; assert d(100, 150) == 0 and d(100, 10) == 90"),
                   {"type": "file_contains", "path": "tests/test_pricing.py", "text": "ValueError"}, {"type": "file_contains", "path": "shop/__init__.py", "text": '"0.2.0"'},
                   {"type": "file_contains", "path": "CHANGELOG.md", "text": "0.2.0"}, SUITE],
        "solution": {
            "shop/pricing.py": REPO["shop/pricing.py"].replace(
                "    return price - price * percent / 100",
                "    if percent < 0:\n        raise ValueError(\"percent must not be negative\")\n    percent = min(percent, 100)\n    return price - price * percent / 100"),
            "tests/test_pricing.py": REPO["tests/test_pricing.py"] + (
                "\n    def test_discount_is_capped(self):\n        self.assertEqual(apply_discount(100, 150), 0)\n"
                "        with self.assertRaises(ValueError):\n            apply_discount(100, -5)\n"),
            "shop/__init__.py": '__version__ = "0.2.0"\n',
            "CHANGELOG.md": "# Changelog\n\n## 0.2.0\n\n- apply_discount caps the percent at 100 and refuses a negative one.\n"},
    },
]


def goals() -> List[Dict[str, Any]]:
    return GOALS
