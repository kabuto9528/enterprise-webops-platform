"""Resolve only mandatory dependencies from the official downloaded update center."""
import argparse
import base64
import json
from pathlib import Path


def version(value):
    return tuple(int(x) for x in value.split("."))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("metadata", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    core = "2.580.2"
    roots = ["workflow-job", "workflow-cps", "workflow-durable-task-step",
             "workflow-basic-steps", "git", "credentials", "ssh-credentials"]
    metadata = json.loads(args.metadata.read_text(encoding="utf-8-sig"))["plugins"]
    found = {}

    def visit(name):
        if name in found:
            return
        item = metadata[name]
        if version(item["requiredCore"]) > version(core):
            raise ValueError(f"{name} requires newer Jenkins: {item['requiredCore']}")
        digest = base64.b64decode(item["sha256"], validate=True).hex()
        if len(digest) != 64 or not item["url"].startswith("https://updates.jenkins.io/download/plugins/"):
            raise ValueError(f"Unexpected official artifact metadata: {name}")
        found[name] = {"name": name, "version": item["version"],
                       "requiredCore": item["requiredCore"], "sha256": digest,
                       "url": item["url"], "requiredDependencies": []}
        for dependency in item.get("dependencies", []):
            if not dependency["optional"]:
                found[name]["requiredDependencies"].append(dependency["name"])
                visit(dependency["name"])

    for root in roots:
        visit(root)
    result = {"jenkinsVersion": core, "resolvedOn": "2026-10-30", "roots": roots,
              "scope": "stage10 minimal scripted Pipeline, Git and SSH credentials; mandatory dependencies only",
              "metadataSource": "https://updates.jenkins.io/current/update-center.actual.json",
              "plugins": [found[name] for name in sorted(found)]}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"PLUGIN_ROOTS={len(roots)} MANDATORY_CLOSURE={len(found)} CORE_COMPATIBLE={core}")
    for item in result["plugins"]:
        print(f"{item['name']}={item['version']}")


if __name__ == "__main__":
    main()
