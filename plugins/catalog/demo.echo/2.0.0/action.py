import json, sys

def main():
    data = json.load(sys.stdin)
    inp = data.get("input", {}).get("text", "")
    out = {"text": f"v2: {inp}"}
    json.dump(out, sys.stdout)
    sys.stdout.flush()

if __name__ == "__main__":
    main()
