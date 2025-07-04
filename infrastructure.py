# infrastructure_analyzer_v15.py

import asyncio
import ast
import os
import re
import shutil
import stat
import tempfile
import time
import json
import xml.etree.ElementTree as ET
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Literal, Set, Any

try:
    import yaml
except ImportError:
    yaml = None

# --- Configuration ---
REPOS_FILE = "repos.txt"
MD_REPORT_FILE = "infrastructure_report.md"
AZURE_LICENSE_FILE = "azure.md" # Renamed for clarity
MAX_CONCURRENT_REPOS = 5
PROVIDER_KEYWORDS = ["azure", "aws", "gcp", "kubernetes", "cloudflare", "digitalocean", "azuread", "azure-native"]
COSMOSDB_KEYWORDS = ["cosmosdb", "documentdb"]
BLOBSTORAGE_KEYWORDS = ["storage.account", "storage.container", "storage.blob"]


# --- Data Structures ---
@dataclass(frozen=True, eq=True)
class InfraResource:
    name: str; resource_type: str; language: Literal["Python", "TypeScript", "Shell", "GitHub Actions"]; source_file: str; size: str = "N/A"

@dataclass
class ServiceInteraction:
    name: str; interaction_type: str; language: str; details: str

@dataclass
class WorkflowSummary:
    name: str; path: str; triggers: str; job_names: List[str]

@dataclass
class RepoReport:
    url: str; name: str
    resources: List[InfraResource] = field(default_factory=list)
    service_interactions: Dict[str, List[ServiceInteraction]] = field(default_factory=dict)
    workflows: List[WorkflowSummary] = field(default_factory=list)


# --- Analysis Functions ---

def analyze_python_file(file_path: Path, repo_root: Path) -> List[InfraResource]:
    resources = []
    try:
        content = file_path.read_text(encoding='utf-8', errors='ignore')
        if not any(provider in content for provider in PROVIDER_KEYWORDS): return []
        tree = ast.parse(content)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Call): continue
            call, func_str = node.value, ast.unparse(node.value.func)
            if "." in func_str and any(provider in func_str for provider in PROVIDER_KEYWORDS):
                if call.args and isinstance(call.args[0], ast.Constant):
                    name, size = call.args[0].value, "N/A"
                    for kw in call.keywords:
                        if kw.arg == 'sku' and isinstance(kw.value, ast.Constant): size = kw.value.value; break
                    resources.append(InfraResource(name, func_str, "Python", str(file_path.relative_to(repo_root)), size))
    except Exception as e: print(f"Warning: Could not parse Python file {file_path}: {e}")
    return resources

def analyze_typescript_file(file_path: Path, repo_root: Path) -> List[InfraResource]:
    resources = []
    try:
        content, provider_pattern = file_path.read_text(encoding='utf-8', errors='ignore'), "|".join(PROVIDER_KEYWORDS)
        pattern = r"new\s+((?:{})\.[\w\.<>]+)\s*\(\s*[\"']([^\"']+)[\"']".format(provider_pattern)
        for match in re.finditer(pattern, content):
            resource_type, name = match.group(1), match.group(2)
            search_area, size_match = content[match.end():match.end() + 200], re.search(r"sku\s*:\s*[\"']([^\"']+)[\"']", content[match.end():match.end() + 200])
            size = size_match.group(1) if size_match else "N/A"
            resources.append(InfraResource(name, resource_type, "TypeScript", str(file_path.relative_to(repo_root)), size))
    except Exception as e: print(f"Warning: Could not parse TypeScript file {file_path}: {e}")
    return resources

def analyze_shell_content(content: str, source_id: str, language: Literal["Shell", "GitHub Actions"]) -> List[InfraResource]:
    resources, pattern = [], r"az\s+([a-z\s-]+?)\s+(create|blob\s+upload).*?(--name|-n|--container-name|-c)\s+(['\"]?)([\w\-\$]+)\4"
    for match in re.finditer(pattern, content, re.IGNORECASE | re.DOTALL):
        action, name = match.group(2), match.group(5)
        resource_type = f"az storage {action}" if "blob" in action else f"az {match.group(1).strip()} create"
        resources.append(InfraResource(name, resource_type, language, source_id))
    return resources

def analyze_sdk_usage(repo_path: Path) -> Dict[str, List[ServiceInteraction]]:
    interactions = {"Cosmos DB": [], "Blob Storage": []}
    for csproj in repo_path.rglob("*.csproj"):
        try:
            content = csproj.read_text(encoding='utf-8')
            if 'Include="Microsoft.Azure.Cosmos"' in content: interactions["Cosmos DB"].append(ServiceInteraction("Unknown", "SDK Usage", ".NET", f"{csproj.relative_to(repo_path)}"))
            if 'Include="Azure.Storage.Blobs"' in content: interactions["Blob Storage"].append(ServiceInteraction("Unknown", "SDK Usage", ".NET", f"{csproj.relative_to(repo_path)}"))
        except Exception as e: print(f"Warning: Could not parse {csproj}: {e}")
    for pkg_json in repo_path.rglob("package.json"):
        try:
            data, dependencies = json.loads(pkg_json.read_text(encoding='utf-8')), {}
            dependencies.update(data.get("dependencies", {})); dependencies.update(data.get("devDependencies", {}))
            if "@azure/cosmos" in dependencies: interactions["Cosmos DB"].append(ServiceInteraction("Unknown", "SDK Usage", "Node.js", f"{pkg_json.relative_to(repo_path)}"))
            if "@azure/storage-blob" in dependencies: interactions["Blob Storage"].append(ServiceInteraction("Unknown", "SDK Usage", "Node.js", f"{pkg_json.relative_to(repo_path)}"))
        except Exception as e: print(f"Warning: Could not parse {pkg_json}: {e}")
    return interactions

def summarize_workflow_file(file_path: Path) -> WorkflowSummary:
    """Parses a GitHub Actions workflow file to get its name, triggers, and jobs."""
    name, triggers, job_names = "Unnamed Workflow", "Unknown", []
    try:
        workflow = yaml.safe_load(file_path.read_text(encoding='utf-8'))
        if isinstance(workflow, dict):
            name = workflow.get("name", file_path.name)
            on_trigger = workflow.get("on", "Unknown")
            if isinstance(on_trigger, str): triggers = on_trigger
            elif isinstance(on_trigger, list): triggers = ", ".join(on_trigger)
            elif isinstance(on_trigger, dict): triggers = ", ".join(on_trigger.keys())
            job_names = list(workflow.get("jobs", {}).keys())
    except Exception as e: print(f"Warning: Could not summarize workflow {file_path}: {e}")
    return WorkflowSummary(name, str(file_path.name), triggers, job_names)


# --- Core Repository Processing ---

async def clone_repo(repo_url: str, target_dir: Path) -> bool:
    if target_dir.exists(): return True
    target_dir.mkdir(parents=True, exist_ok=True)
    process = await asyncio.create_subprocess_exec("git", "clone", "--depth=1", repo_url, str(target_dir), stderr=asyncio.subprocess.PIPE)
    _, stderr = await process.communicate()
    if process.returncode != 0: print(f"Error cloning {repo_url}: {stderr.decode().strip()}"); return False
    return True

async def analyze_repository(repo_url: str, work_dir: Path) -> RepoReport:
    repo_name, repo_path = repo_url.split('/')[-1], work_dir / repo_url.split('/')[-1]
    report = RepoReport(url=repo_url, name=repo_name)

    print(f"[{repo_name}] Cloning repository...")
    if not await clone_repo(repo_url, repo_path): raise RuntimeError(f"Failed to clone {repo_url}")
    print(f"[{repo_name}] Searching for infrastructure files and SDKs...")
    
    found_resources, workflow_files = [], []
    for file_path in repo_path.rglob("*"):
        if any(part in file_path.parts for part in ["node_modules", ".venv", "target", "dist", "build"]) or not file_path.is_file(): continue
        
        if file_path.suffix == ".py": found_resources.extend(analyze_python_file(file_path, repo_path))
        elif file_path.suffix == ".ts": found_resources.extend(analyze_typescript_file(file_path, repo_path))
        elif file_path.suffix == ".sh": found_resources.extend(analyze_shell_content(file_path.read_text(encoding='utf-8', errors='ignore'), str(file_path.relative_to(repo_path)), "Shell"))
        elif file_path.suffix in [".yml", ".yaml"] and ".github/workflows" in str(file_path): workflow_files.append(file_path)

    report.resources = sorted(list(set(found_resources)), key=lambda r: (r.language, r.resource_type, r.name))
    report.service_interactions = analyze_sdk_usage(repo_path)
    if yaml:
        report.workflows = [summarize_workflow_file(f) for f in workflow_files]
        for f in workflow_files:
            try:
                workflow = yaml.safe_load(f.read_text(encoding='utf-8'))
                for job_id, job in workflow.get("jobs", {}).items():
                    for i, step in enumerate(job.get("steps", [])):
                        if isinstance(step, dict) and "azure/cli" in step.get("uses", ""):
                            script = step.get("with", {}).get("inlineScript", "")
                            if script: report.resources.extend(analyze_shell_content(script, f"{f.relative_to(repo_path)} (Job: {job_id})", "GitHub Actions"))
            except Exception as e: print(f"Warning: Could not parse workflow {f} for CLI steps: {e}")

    for res in report.resources:
        res_type_lower = res.resource_type.lower()
        if any(keyword in res_type_lower for keyword in COSMOSDB_KEYWORDS): report.service_interactions["Cosmos DB"].append(ServiceInteraction(res.name, "IaC Resource", res.language, res.resource_type))
        if any(keyword in res_type_lower for keyword in BLOBSTORAGE_KEYWORDS): report.service_interactions["Blob Storage"].append(ServiceInteraction(res.name, "IaC Resource", res.language, res.resource_type))

    print(f"[{repo_name}] Analysis complete.")
    return report

# --- Report Generation & Main ---

def write_md_report(reports: List[RepoReport], filename: str):
    with open(filename, 'w', encoding='utf-8') as f:
        f.write(f"# Infrastructure Report\n\n_Generated on {time.ctime()}_\n\n## 📜 Overall Summary\n\n")
        
        # --- NEW: Licensing Section ---
        f.write("### Licensing\n\n")
        try:
            with open(AZURE_LICENSE_FILE, 'r', encoding='utf-8') as license_file:
                f.write(license_file.read())
            f.write("\n\n_For official terms, please refer to the [Microsoft Azure Legal Information](https://azure.microsoft.com/en-us/support/legal/)._\n")
        except FileNotFoundError:
            f.write(f"**Note:** The license file `{AZURE_LICENSE_FILE}` was not found.\n")
        f.write("\n---\n\n")

        # --- Service-Specific Summaries ---
        for service_name in ["Cosmos DB", "Blob Storage"]:
            all_interactions = [(inter, r.name) for r in reports for inter in r.service_interactions.get(service_name, [])]
            if all_interactions:
                f.write(f"### {service_name} Analysis\n\n| Repository | Interaction Type | Detected Language | Details |\n|---|---|---|---|\n")
                for interaction, repo_name in sorted(all_interactions, key=lambda x: (x[1], x[0].interaction_type)):
                    f.write(f"| {repo_name} | {interaction.interaction_type} | {interaction.language} | `{interaction.details}` |\n")
                f.write("\n---\n\n")
        
        all_resources = [res for r in reports for res in r.resources]
        f.write("### General Resource Count by Type\n\n| Resource Type | Count |\n|---|---|\n")
        for res_type, count in Counter(res.resource_type for res in all_resources).most_common(): f.write(f"| `{res_type}` | {count} |\n")
        
        f.write("\n---\n\n## 📚 Repository Details\n\n")
        for report in sorted(reports, key=lambda r: r.name):
            f.write(f"### [{report.name}]({report.url})\n\n")
            has_content = False
            if report.workflows:
                has_content = True
                f.write("#### GitHub Workflow Summary\n\n| Workflow File | Triggers | Job Names |\n|---|---|---|\n")
                for wf in sorted(report.workflows, key=lambda x: x.name): f.write(f"| `{wf.path}` | {wf.triggers} | {', '.join(wf.job_names)} |\n")
                f.write("\n")
            
            service_interactions = [(s, i) for s, il in report.service_interactions.items() for i in il]
            if service_interactions:
                has_content = True
                f.write("#### Detected Service Interactions\n\n| Service | Type | Language | Details |\n|---|---|---|---|\n")
                for service, inter in sorted(service_interactions, key=lambda x: (x[0], x[1].interaction_type)): f.write(f"| {service} | {inter.interaction_type} | {inter.language} | `{inter.details}` |\n")
                f.write("\n")

            if report.resources:
                has_content = True
                f.write("#### All Infrastructure Resources\n\n| Language / Source | Resource Type | Name |\n|---|---|---|\n")
                for res in report.resources: f.write(f"| {res.language} | `{res.resource_type}` | `{res.name}` |\n")
                f.write("\n")
            
            if not has_content: f.write("_No infrastructure resources or specific service interactions found._\n\n")

def load_repositories(filename: str) -> List[str]:
    if not Path(filename).exists(): return []
    with open(filename, 'r') as f: return [line.strip() for line in f if line.strip() and not line.startswith('#')]

def on_rm_error(func, path, exc_info):
    os.chmod(path, stat.S_IWRITE); func(path)

async def main():
    repos = load_repositories(REPOS_FILE)
    if not repos: print(f"Error: `{REPOS_FILE}` is empty or not found."); return
    temp_dir = Path(tempfile.mkdtemp(prefix="infra_analyzer_"))
    print(f"Using temporary directory: {temp_dir}")
    try:
        repo_semaphore = asyncio.Semaphore(MAX_CONCURRENT_REPOS)
        async def constrained_analyzer(repo_url: str):
            async with repo_semaphore: return await analyze_repository(repo_url, temp_dir)
        tasks = [constrained_analyzer(url) for url in repos]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        successful_reports = [r for r in results if not isinstance(r, Exception)]
        if successful_reports: write_md_report(successful_reports, MD_REPORT_FILE)
    finally:
        if temp_dir.exists(): shutil.rmtree(temp_dir, onerror=on_rm_error)
        print("Successfully cleaned up temporary directory.")
    print("Done.")

if __name__ == "__main__":
    if shutil.which("git") is None: print("Error: Git is not installed or not in your PATH.")
    elif yaml is None: print("Required library PyYAML is missing. Please run `pip install PyYAML` to enable all features.")
    else: asyncio.run(main())