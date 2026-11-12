"""A stand-in for the OCR worker: reads nothing and says every image holds the word TRE3G, in the lines the real worker prints."""
import json
import sys

for path in sys.argv[1:]:
    print(json.dumps({"image": path, "lines": [{"text": "TRE3G", "score": 0.99, "box": [1, 2, 30, 40]}]}), flush=True)
print(json.dumps({"event": "done"}), flush=True)
