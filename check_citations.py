"""check_citations.py - STUDENT IMPLEMENTS `check`.   Runs INSIDE the sandbox (standard library only).

research.py uploads this file to the sandbox and the lead agent runs it with the `execute` tool:
    python3 /tmp/work/research/check_citations.py [report.md] [sources.json]
It must exit 0 and print "OK: ..." when the report is consistent, else print each problem and exit 1.
"""
import json
import re
import sys

REPORT = "/tmp/work/report/report.md"
SOURCES = "/tmp/work/research/sources.json"


def check(report_text, sources):
    """Return a list of problem strings (empty list = OK).

    Implements 6 rules:
      1. sources not empty
      2. each source: n is int, url starts with http:// or https://, no duplicate urls
      3. report has ## References heading
      4. every [n] in body exists in sources, every source is cited at least once
      5. exactly one reference line per source, properly numbered
      6. each reference line has exactly one URL matching sources[n].url
    """
    problems = []

    # Rule 1: sources not empty
    if not sources:
        return ["no sources in sources.json"]

    if not isinstance(sources, list):
        return ["sources must be a JSON list"]

    # Rule 2: validate each source entry
    seen_urls = set()
    source_by_n = {}

    for i, source in enumerate(sources):
        if not isinstance(source, dict):
            problems.append(f"source[{i}] is not an object")
            continue

        # Check 'n' is an integer
        n = source.get("n")
        if not isinstance(n, int):
            problems.append(f"source[{i}] has non-integer n: {n!r}")

        # Check URL
        url = source.get("url", "")
        if not url:
            problems.append(f"source[{i}] has no url")
        elif not url.startswith("http://") and not url.startswith("https://"):
            problems.append(f"source[{i}] url does not start with http:// or https://: {url}")
        elif url in seen_urls:
            problems.append(f"duplicate url: {url}")
        else:
            seen_urls.add(url)

        # Track by n for later checks
        if isinstance(n, int):
            if n in source_by_n:
                problems.append(f"duplicate source number n={n}")
            source_by_n[n] = source

    # Early exit if sources have critical issues
    if problems:
        return problems

    # Rule 3: split report at ## References
    ref_pattern = re.compile(r"(?m)^##[ \t]+References[ \t]*$")
    matches = list(ref_pattern.finditer(report_text))

    if not matches:
        return ["missing ## References heading"]

    body = report_text[:matches[-1].start()]

    # Rule 4: find all [n] citations in body (not in reference list)
    # Exclude code blocks and markdown links [n](url)
    code_pattern = re.compile(r"(```[\s\S]*?```|`[^`\n]*`)")
    link_pattern = re.compile(r"\[[\d,\- ]+\]\([^)]+\)")  # [1,2](url) or [1-3](url)

    # Remove code blocks and markdown links from consideration
    cleaned_body = code_pattern.sub("", body)
    cleaned_body = link_pattern.sub("", cleaned_body)

    # Find citations: [n] or [n,m] or [n-m]
    citation_pattern = re.compile(r"\[(\d+(?:\s*[,–-]\s*\d+)*)\]")

    cited_numbers = set()
    for match in citation_pattern.finditer(cleaned_body):
        group = match.group(1)
        # Handle grouped citations [1,2] or [1-3]
        for part in re.split(r"\s*,\s*", group):
            span = re.match(r"(\d+)\s*[-–]\s*(\d+)", part)
            if span:
                a, b = int(span.group(1)), int(span.group(2))
                if a <= b:
                    cited_numbers.update(range(a, b + 1))
            else:
                try:
                    cited_numbers.add(int(part))
                except ValueError:
                    pass

    # Check every cited number exists in sources
    for n in sorted(cited_numbers):
        if n not in source_by_n:
            problems.append(f"[{n}] cited in body but missing from sources.json")

    # Check every source is cited at least once
    for n in sorted(source_by_n.keys()):
        if n not in cited_numbers:
            problems.append(f"source n={n} never cited in body")

    # Rule 5 & 6: parse reference lines
    ref_section = report_text[matches[-1].start():]

    # Find all lines starting with [n]
    ref_line_pattern = re.compile(r"^\[(\d+)\][ \t]*(.+)$", re.MULTILINE)

    ref_lines = {}
    for match in ref_line_pattern.finditer(ref_section):
        line_n = int(match.group(1))
        line_content = match.group(2).strip()

        if line_n in ref_lines:
            problems.append(f"duplicate reference number [{line_n}]")
        else:
            ref_lines[line_n] = line_content

    # Check each source has exactly one reference line
    for n in sorted(source_by_n.keys()):
        if n not in ref_lines:
            problems.append(f"source n={n} has no reference line")

    # Check reference lines have exactly one URL matching source
    url_pattern = re.compile(r"https?://\S+")

    for n, line_content in ref_lines.items():
        if n not in source_by_n:
            problems.append(f"reference [{n}] does not correspond to any source")
            continue

        expected_url = source_by_n[n].get("url", "")

        # Find URLs in line
        urls_in_line = url_pattern.findall(line_content)

        if not urls_in_line:
            problems.append(f"reference [{n}] has no URL")
        elif len(urls_in_line) > 1:
            problems.append(f"reference [{n}] has multiple URLs: {urls_in_line}")
        elif urls_in_line[0] != expected_url:
            problems.append(f"reference [{n}] URL mismatch: expected {expected_url}, got {urls_in_line[0]}")

    return problems


def main(argv):
    report_path = argv[1] if len(argv) > 1 else REPORT
    sources_path = argv[2] if len(argv) > 2 else SOURCES
    try:
        with open(report_path, encoding="utf-8") as f:
            report = f.read()
        with open(sources_path, encoding="utf-8") as f:
            sources = json.load(f)
    except (OSError, ValueError) as exc:
        print(f"cannot read inputs: {exc}")
        return 1
    problems = check(report, sources)
    if problems:
        print("\n".join(problems))
        return 1
    print(f"OK: {len(sources)} sources, all citations resolve")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
