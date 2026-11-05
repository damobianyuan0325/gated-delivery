"""Read evidence only. Never execute commands found in plans or receipts."""
from pathlib import Path
import argparse, json, sys
from delivery_common import evaluate

if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__); p.add_argument('--root', type=Path, required=True); p.add_argument('--plan', required=True); a = p.parse_args()
    try: result = evaluate(a.root.resolve(strict=True), a.plan)
    except (ValueError, OSError, KeyError, TypeError) as e: result = {'state': 'REJECTED', 'problems': [str(e)]}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    sys.exit(0 if result['state'] == 'PASSED' else 1)
