"""Frontend checks that a brace count cannot give you.

A previous release shipped a dashboard whose script block did not parse. The
server stayed healthy, so every API check passed while the page itself was
blank. These checks run the actual JavaScript through `node --check` and verify
the i18n table by parsing it, rather than inferring either from the HTML text.

Requires node on PATH. Skips with a loud warning if it is missing, because a
silent skip would recreate exactly the blind spot this exists to close.
"""

import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HTML = ROOT / "static" / "dashboard.html"

failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(("PASS  " if cond else "FAIL  ") + name + (f"\n      {detail}" if detail and not cond else ""))
    if not cond:
        failures.append(name)


def script_blocks(html: str) -> list[str]:
    return re.findall(r"<script>(.*?)</script>", html, re.S)


def main() -> None:
    html = HTML.read_text()
    blocks = script_blocks(html)
    print(f"script blocks: {len(blocks)} (sizes: {[len(b) for b in blocks]})\n")
    check("dashboard has script blocks", bool(blocks))

    node = shutil.which("node")
    if not node:
        print("\n!! node not found -- JavaScript was NOT parsed.")
        print("!! Install it (brew install node) before trusting this run.")
        failures.append("node unavailable")
    else:
        with tempfile.TemporaryDirectory() as tmp:
            for i, block in enumerate(blocks):
                f = Path(tmp) / f"block_{i}.js"
                f.write_text(block)
                r = subprocess.run([node, "--check", str(f)],
                                   capture_output=True, text=True)
                check(f"script block {i} parses",
                      r.returncode == 0,
                      (r.stderr or "").strip()[:400])

    # --- i18n -------------------------------------------------------------
    # Evaluate the real object rather than regexing it: keys inside translated
    # prose ("no hay jugadores: ...") produced false mismatches before.
    if node and "const I18N" in html:
        start = html.index("const I18N")
        eq = html.index("{", start)
        depth, i = 0, eq
        while True:
            if html[i] == "{":
                depth += 1
            elif html[i] == "}":
                depth -= 1
                if depth == 0:
                    break
            i += 1
        obj = html[eq:i + 1]

        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "i18n.js"
            f.write_text(f"const I18N = {obj};\n"
                         "console.log(JSON.stringify(Object.fromEntries("
                         "Object.entries(I18N).map(([k,v])=>[k,Object.keys(v)]))));")
            r = subprocess.run([node, str(f)], capture_output=True, text=True)
            if r.returncode != 0:
                check("i18n table evaluates", False, (r.stderr or "").strip()[:400])
            else:
                keys = json.loads(r.stdout)
                langs = sorted(keys)
                sizes = {k: len(v) for k, v in keys.items()}
                print(f"\nlanguages: {langs}  sizes: {sizes}")
                base = set(keys.get("en", []))
                check("English table is populated", bool(base))
                for code, ks in keys.items():
                    missing = base - set(ks)
                    extra = set(ks) - base
                    check(f"{code} matches English key set",
                          not missing and not extra,
                          f"missing={sorted(missing)} extra={sorted(extra)}")

                # Every i18n attribute must resolve, including placeholders.
                used = set(re.findall(r'data-i18n="([^"]+)"', html))
                undef = sorted(used - base)
                check("every data-i18n key is defined", not undef, f"undefined={undef}")

                ph = set(re.findall(r'data-i18n-ph="([^"]+)"', html))
                undef_ph = sorted(ph - base)
                check("every data-i18n-ph key is defined", not undef_ph,
                      f"undefined={undef_ph}")

                # t('key') literals in the script must resolve too. Dynamic
                # prefixes like t('sig' + signal) are excluded by name.
                DYNAMIC = {"sig"}
                lit = set(re.findall(
                    r"(?<![A-Za-z0-9_$])t\(\s*'([A-Za-z_][A-Za-z0-9_]*)'", html))
                undef_lit = sorted(lit - base - DYNAMIC)
                check("every t('key') literal is defined", not undef_lit,
                      f"undefined={undef_lit}")

    # --- No native dialogs ------------------------------------------------
    # A browser offers "prevent this page from creating additional dialogs"
    # after a couple of them, and once accepted alert/confirm/prompt stop
    # working for the rest of the session. confirm() then returns false, which
    # merely fails safe, but prompt() returns null -- so an admin password
    # reset silently does nothing with no error to show. Use Ask.open().
    code = "\n".join(script_blocks(html))
    code = re.sub(r"/\*.*?\*/", "", code, flags=re.S)          # block comments
    code = re.sub(r"^\s*//.*$", "", code, flags=re.M)          # line comments
    for fn in ("alert", "confirm", "prompt"):
        hits = re.findall(rf"(?<![.\w$]){fn}\s*\(", code)
        check(f"no native {fn}() in the dashboard", not hits,
              f"{len(hits)} call(s)")

    # --- Structure --------------------------------------------------------
    # Comments discuss these tags by name, so count only real markup.
    markup = re.sub(r"<!--.*?-->", "", html, flags=re.S)
    check("noscript tags are balanced",
          markup.count("<noscript>") == markup.count("</noscript>"))
    for el in ("landing", "app"):
        check(f"#{el} element present", f'id="{el}"' in markup)

    # The crawler-facing summary must stay reachable without JavaScript: it is
    # hidden by a .js class, never by a default display:none.
    if "seo-summary" in markup:
        # It must be hidden by `.js #seo-summary`, never by a bare
        # `#seo-summary { display:none }` -- the latter would hide it from the
        # JS-less readers it exists for.
        hidden_by_js = ".js #seo-summary" in html
        hidden_always = re.search(r"(?<!\.js )#seo-summary\s*\{[^}]*display:\s*none", html)
        check("seo summary is hidden only when JS runs",
              hidden_by_js and not hidden_always)
        check("seo summary is not trapped inside noscript",
              "<noscript>" not in markup)

    print()
    if failures:
        print(f"{len(failures)} FAILURE(S): {failures}")
        sys.exit(1)
    print("all frontend checks passed")


if __name__ == "__main__":
    main()
