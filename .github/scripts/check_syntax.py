"""Check executable source syntax without running installers or browser code."""

import ast
from html.parser import HTMLParser
from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parents[2]


class InlineScripts(HTMLParser):
    def __init__(self):
        super().__init__()
        self.active = False
        self.parts = []
        self.scripts = []

    def handle_starttag(self, tag, attrs):
        if tag == "script":
            attrs = dict(attrs)
            self.active = "src" not in attrs and attrs.get("type", "text/javascript") in (
                "text/javascript", "application/javascript", "module"
            )
            self.parts = []

    def handle_data(self, data):
        if self.active:
            self.parts.append(data)

    def handle_endtag(self, tag):
        if tag == "script" and self.active:
            self.scripts.append("".join(self.parts))
            self.active = False


def main():
    tracked = subprocess.check_output(
        ["git", "-c", f"safe.directory={ROOT}", "ls-files", "-z"], cwd=ROOT
    ).decode().split("\0")
    for name in filter(None, tracked):
        path = ROOT / name
        if path.suffix == ".sh":
            print(f"Checking shell: {name}", flush=True)
            subprocess.run(["bash", "-n", str(path)], check=True)
            # Block syntax and correctness errors; legacy style warnings are non-blocking.
            subprocess.run(["shellcheck", "--severity=error", str(path)], check=True)
        elif path.suffix == ".php":
            subprocess.run(["php", "-l", str(path)], check=True)
        elif path.suffix == ".js":
            subprocess.run(["node", "--check", str(path)], check=True)
        elif path.suffix == ".html":
            parser = InlineScripts()
            parser.feed(path.read_text())
            for index, script in enumerate(parser.scripts, 1):
                print(f"Checking JavaScript: {name}, inline script {index}", flush=True)
                subprocess.run(["node", "--check"], input=script, text=True, check=True)
        elif path.suffix == ".py":
            ast.parse(path.read_text(), filename=str(path))

    subprocess.run([
        "php", "-r",
        'foreach (["pdo_mysql", "curl", "json"] as $ext) {'
        ' if (!extension_loaded($ext)) {fwrite(STDERR, "Missing PHP extension: $ext\\n"); exit(1);}'
        '}'
    ], check=True)
    print("All syntax checks passed.", flush=True)


if __name__ == "__main__":
    main()
