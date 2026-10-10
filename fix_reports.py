"""Fix existing reports: handle Source [n](url) format and regenerate properly."""
import json
import re
from pathlib import Path

def fix_report(report_path, sources_path):
    """Fix a single report by standardizing citations and regenerating references."""
    # Read files
    with open(report_path, 'r', encoding='utf-8') as f:
        report = f.read()
    with open(sources_path, 'r', encoding='utf-8') as f:
        sources = json.load(f)

    # Build url -> n mapping (normalize URLs for comparison)
    url_to_n = {}
    n_to_source = {}
    for s in sources:
        n = s['n']
        url = s.get('url', '')
        url_to_n[url] = n
        n_to_source[n] = s

        # Also handle arxiv.org/abs/ vs arxiv.org/html/
        if 'arxiv.org/abs/' in url:
            html_url = url.replace('/abs/', '/html/')
            url_to_n[html_url] = n
        elif 'arxiv.org/html/' in url:
            abs_url = url.replace('/html/', '/abs/')
            url_to_n[abs_url] = n

    # Find body (before ## References)
    ref_match = re.search(r'(?m)^##[ \t]+References[ \t]*$', report)
    if not ref_match:
        print(f"  No ## References found")
        return False

    body = report[:ref_match.start()]

    # Replace (Source [n](url)) with [n]
    # Pattern: (Source [number](URL))
    source_link_pattern = re.compile(r'\(Source \[(\d+)\]\([^)]+\)\)')

    cited_nums = set()
    def replace_source_link(match):
        cited_nums.add(int(match.group(1)))
        return f"[{match.group(1)}]"

    new_body = source_link_pattern.sub(replace_source_link, body)

    # Also find standalone [n] citations
    # Exclude code blocks and markdown links [n](url)
    code_pattern = re.compile(r"(```[\s\S]*?```|`[^`\n]*`)")
    link_pattern = re.compile(r"\[[\d,\- ]+\]\([^)]+\)")

    cleaned_body = code_pattern.sub("", new_body)
    cleaned_body = link_pattern.sub("", cleaned_body)

    citation_pattern = re.compile(r"\[(\d+(?:\s*[,–-]\s*\d+)*)\]")
    for match in citation_pattern.finditer(cleaned_body):
        group = match.group(1)
        for part in re.split(r"\s*,\s*", group):
            span = re.match(r"(\d+)\s*[-–]\s*(\d+)", part)
            if span:
                a, b = int(span.group(1)), int(span.group(2))
                cited_nums.update(range(a, b + 1))
            else:
                try:
                    cited_nums.add(int(part))
                except ValueError:
                    pass

    if not cited_nums:
        print(f"  No citations found")
        return False

    print(f"  Found citations: {sorted(cited_nums)}")

    # Filter sources to only cited ones
    cited_sources = [s for s in sources if s['n'] in cited_nums]

    if not cited_sources:
        print(f"  No valid cited sources")
        return False

    # Renumber sources in order of first appearance in body
    new_sources = []
    new_n_map = {}
    for i, s in enumerate(cited_sources, 1):
        old_n = s['n']
        new_n_map[old_n] = i
        new_s = dict(s)
        new_s['n'] = i
        new_sources.append(new_s)

    print(f"  Renumbered {len(new_sources)} sources")

    # Rewrite all citations in body
    def rewrite_citation(match):
        nums = []
        for part in re.split(r"\s*,\s*", match.group(1)):
            span = re.match(r"(\d+)\s*[-–]\s*(\d+)", part)
            if span:
                a, b = int(span.group(1)), int(span.group(2))
                nums.extend(range(a, b + 1))
            else:
                nums.append(int(part))
        return "[" + "][".join(str(new_n_map.get(n, n)) for n in nums) + "]"

    new_body = citation_pattern.sub(rewrite_citation, new_body)

    # Regenerate References section
    ref_lines = []
    for s in new_sources:
        title = " ".join(str(s.get('title', 'Untitled')).split())
        url = s.get('url', '')
        date = s.get('date', 'n.d.')
        source_type = s.get('source', 'web')
        ref_lines.append(f"[{s['n']}] {title}. {source_type}. {url} ({date})")

    new_report = new_body + "\n\n## References\n" + "\n".join(ref_lines) + "\n"

    # Write files
    with open(report_path, 'w', encoding='utf-8') as f:
        f.write(new_report)
    with open(sources_path, 'w', encoding='utf-8') as f:
        json.dump(new_sources, f, ensure_ascii=False, indent=2)

    print(f"  Written: {len(new_sources)} sources")
    return True


def main():
    reports_dir = Path(__file__).parent / "reports"

    topics = [
        ("survey about world model", "survey-about-world-model"),
        ("survey about reinforcement learning for LLM reasoning", "survey-about-reinforcement-learning-for-llm-reasoning"),
        ("survey about LLM agents and tool use", "survey-about-llm-agents-and-tool-use"),
        ("survey about video and multimodal generation", "survey-about-video-and-multimodal-generation"),
        ("survey about efficient inference and small language models", "survey-about-efficient-inference-and-small-language-models"),
    ]

    print("Fixing reports...")
    for topic, slug in topics:
        report_path = reports_dir / f"{slug}.md"
        sources_path = reports_dir / f"{slug}.sources.json"
        meta_path = reports_dir / f"{slug}.meta.json"

        if not report_path.exists():
            print(f"\n{topic}: SKIP - report not found")
            continue

        print(f"\n{topic}:")

        # Update meta.json
        if sources_path.exists():
            with open(sources_path, 'r', encoding='utf-8') as f:
                sources = json.load(f)
            families = sorted(set(s.get('source', '').lower() for s in sources if s.get('source')))

            if meta_path.exists():
                with open(meta_path, 'r', encoding='utf-8') as f:
                    meta = json.load(f)
                meta['source_families'] = families
                meta['n_sources'] = len(sources)
                meta['topic'] = topic
                with open(meta_path, 'w', encoding='utf-8') as f:
                    json.dump(meta, f, indent=2, ensure_ascii=False)
                print(f"  Families: {families}")

        if fix_report(report_path, sources_path):
            print(f"  OK")
        else:
            print(f"  Failed to fix")


if __name__ == "__main__":
    main()
