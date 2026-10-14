"""Generate a portable HTML report without contacting model services."""
import argparse
import json
from pathlib import Path

CONFIG_PATH = Path(__file__).with_name("config.example.yaml")
OUTPUT_ROOT = None
RESULTS_PATH = None


def build_report(results_path, output_path=None):
    results_path = Path(results_path)
    data = json.loads(results_path.read_text(encoding="utf-8"))
    public = {key: data.get(key) for key in ("started_at", "finished_at", "status", "totals", "summary", "data_source")}
    settings = data.get("settings", {})
    public["environment"] = data.get("environment") or {key: settings.get(key) for key in ("name", "version", "session_id", "tokenizer", "environment")}
    template = (Path(__file__).parent / "templates" / "report.html").read_text(encoding="utf-8")
    static = Path(__file__).with_name("static")
    template = template.replace("__REPORT_CSS__", (static / "report.css").read_text(encoding="utf-8"))
    template = template.replace("__REPORT_JS__", (static / "results.js").read_text(encoding="utf-8"))
    payload = json.dumps(public, ensure_ascii=False, allow_nan=False).replace("<", "\\u003c")
    output = Path(output_path) if output_path else results_path.with_name("report.html")
    output.write_text(template.replace("__RESULT_DATA__", payload), encoding="utf-8")
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results", type=Path, default=RESULTS_PATH)
    parser.add_argument("--config", type=Path, default=CONFIG_PATH)
    parser.add_argument("--output-root", type=Path, default=OUTPUT_ROOT)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    source = args.results
    if source is None:
        from benchmark import load_config, resolve_output
        config = load_config(args.config)
        output, _ = resolve_output(config, args.config.parent, args.output_root)
        source = output / "results.json"
    print(build_report(source, args.output))


if __name__ == "__main__":
    main()
