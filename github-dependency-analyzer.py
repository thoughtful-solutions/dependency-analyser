# fast_analyzer.py (v7 - final fix for PyPI API edge case)

import asyncio
import aiofiles
import csv
import json
import os
import shutil
import re
import time
import xml.etree.ElementTree as ET
import tempfile
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import aiohttp

# --- Configuration ---

# Files used by the script
REPOS_FILE = "repos.txt"
MAPPING_FILE = "dependency_mapping.csv"
CSV_REPORT_FILE = "dependency_report.csv"
MD_REPORT_FILE = "dependency_report.md"
MISSING_MAPPING_FILE = "missing-dependency-mapping.csv"

# Limits to avoid excessive API calls
MAX_CONCURRENT_REPOS = 5
MAX_CONCURRENT_API_CALLS = 10
API_CALL_LIMIT_PER_TYPE = {
    'python': 25,
    'javascript': 50,
    'java': 25,
    'dotnet': 25
}


# --- Data Structures ---

@dataclass
class Dependency:
    """Represents a single dependency."""
    name: str
    version: str
    type: str
    license: str = "Unknown"
    url: str = ""

@dataclass
class RepoReport:
    """Holds the analysis results for a single repository."""
    url: str
    name: str
    license: str = "Unknown"
    description: str = "No description available."
    types: List[str] = field(default_factory=list)
    dependencies: List[Dependency] = field(default_factory=list)


# --- File Parsers ---

def parse_python_deps(repo_path: Path) -> Dict[str, str]:
    """Parses Python dependency files."""
    deps = {}
    # requirements.txt
    for req_file in repo_path.rglob('requirements.txt'):
        try:
            with open(req_file, 'r', encoding='utf-8', errors='ignore') as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith('#'):
                        match = re.match(r'([a-zA-Z0-9_.-]+)', line)
                        if match:
                            deps[match.group(1)] = 'latest'
        except Exception as e:
            print(f"Warning: Could not parse {req_file}: {e}")
    # pyproject.toml
    for toml_file in repo_path.rglob('pyproject.toml'):
        try:
            with open(toml_file, 'r', encoding='utf-8', errors='ignore') as f:
                content = f.read()
                matches = re.findall(r'\[tool\.poetry\.dependencies\]\s*([^\[]+)', content, re.DOTALL)
                if not matches:
                    matches = re.findall(r'dependencies\s*=\s*\[\s*([^\]]+)\]', content, re.DOTALL)
                for block in matches:
                    for line in block.split('\n'):
                        line = line.strip().strip('"\'')
                        if line and not line.startswith('#'):
                             match = re.match(r'([a-zA-Z0-9_.-]+)', line)
                             if match:
                                 deps[match.group(1)] = 'latest'
        except Exception as e:
            print(f"Warning: Could not parse {toml_file}: {e}")

    return deps

def parse_js_deps(repo_path: Path) -> Dict[str, str]:
    """Parses package.json for JavaScript dependencies."""
    deps = {}
    for pkg_file in repo_path.rglob('package.json'):
        try:
            with open(pkg_file, 'r', encoding='utf-8', errors='ignore') as f:
                data = json.load(f)
                if isinstance(data, dict):
                    deps.update(data.get('dependencies', {}))
                    deps.update(data.get('devDependencies', {}))
        except Exception as e:
            print(f"Warning: Could not parse {pkg_file}: {e}")
    return deps

def parse_java_deps(repo_path: Path) -> Dict[str, str]:
    """Parses pom.xml and build.gradle for Java dependencies."""
    deps = {}
    # pom.xml (Maven)
    for pom_file in repo_path.rglob('pom.xml'):
        try:
            tree = ET.parse(pom_file)
            ns = {'m': 'http://maven.apache.org/POM/4.0.0'}
            for dep in tree.findall('.//m:dependency', ns):
                group_id = dep.findtext('m:groupId', '', ns)
                artifact_id = dep.findtext('m:artifactId', '', ns)
                version = dep.findtext('m:version', '${project.version}', ns)
                if group_id and artifact_id:
                    deps[f"{group_id}:{artifact_id}"] = version
        except ET.ParseError as e:
            print(f"Warning: Could not parse {pom_file}: {e}")

    # build.gradle (Gradle)
    for gradle_file in repo_path.rglob('build.gradle'):
         try:
            with open(gradle_file, 'r', encoding='utf-8', errors='ignore') as f:
                content = f.read()
                matches = re.findall(r'(?:implementation|compile|api)\s*[\'"]([^\'"]+)[\'"]', content)
                for match in matches:
                    parts = match.split(':')
                    if len(parts) >= 2:
                        name = f"{parts[0]}:{parts[1]}"
                        version = parts[2] if len(parts) > 2 else 'latest'
                        deps[name] = version
         except Exception as e:
             print(f"Warning: Could not parse {gradle_file}: {e}")

    return deps

def parse_dotnet_deps(repo_path: Path) -> Dict[str, str]:
    """Parses .csproj files for .NET dependencies."""
    deps = {}
    for csproj_file in repo_path.rglob('*.csproj'):
        try:
            with open(csproj_file, 'r', encoding='utf-8', errors='ignore') as f:
                content = f.read()
            matches = re.findall(r'<PackageReference\s+Include="([^"]+)"\s+Version="([^"]+)"', content)
            for pkg, version in matches:
                deps[pkg] = version
        except Exception as e:
            print(f"Warning: Could not parse {csproj_file}: {e}")

    return deps


# --- Language & License Identification ---

def determine_repo_types(repo_path: Path) -> List[str]:
    """Determines repository types based on file extensions and names."""
    types = set()
    if next(repo_path.rglob("*.py"), None) or next(repo_path.rglob("requirements.txt"), None):
        types.add("python")
    if next(repo_path.rglob("package.json"), None):
        types.add("javascript")
    if next(repo_path.rglob("pom.xml"), None) or next(repo_path.rglob("build.gradle"), None):
        types.add("java")
    if next(repo_path.rglob("*.csproj"), None):
        types.add("dotnet")
    return list(types)

def identify_repo_license(repo_path: Path) -> str:
    """Identifies the license of the repository from common license files."""
    for license_filename in ["LICENSE", "LICENSE.md", "LICENSE.txt", "COPYING"]:
        for license_file in repo_path.glob(f"**/{license_filename}"):
            try:
                with open(license_file, 'r', encoding='utf-8', errors='ignore') as f:
                    content = f.read().lower()
                if "mit license" in content: return "MIT"
                if "apache license" in content: return "Apache-2.0"
                if "gnu general public license" in content: return "GPL"
                if "mozilla public license" in content: return "MPL-2.0"
                return "Custom"
            except Exception:
                continue
    return "Unknown"

def extract_repo_description(repo_path: Path) -> str:
    """Extracts a description from the README.md file."""
    for readme_filename in ["README.md", "readme.md"]:
        readme_file = repo_path / readme_filename
        if readme_file.exists():
            try:
                with open(readme_file, 'r', encoding='utf-8', errors='ignore') as f:
                    content = f.read()
                    # Find the first paragraph that isn't a title
                    paragraphs = re.split(r'\n\s*\n', content)
                    for p in paragraphs:
                        p_clean = p.strip()
                        if p_clean and not p_clean.startswith('#'):
                            # Remove markdown formatting
                            p_clean = re.sub(r'(\*\*|\*|__|_|`|\[.*\]\(.*\))', '', p_clean)
                            return p_clean[:300] + '...' if len(p_clean) > 300 else p_clean
            except Exception as e:
                 print(f"Warning: Could not read {readme_file}: {e}")
    return "No description available."


# --- API Client (MODIFIED) ---

class APIClient:
    """A client to fetch dependency data from various package managers."""

    def __init__(self, semaphore: asyncio.Semaphore):
        self._session: Optional[aiohttp.ClientSession] = None
        self.semaphore = semaphore

    async def __aenter__(self):
        self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15))
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self._session and not self._session.closed:
            await self._session.close()

    async def _get(self, url: str) -> Optional[Dict]:
        """Performs a GET request and returns JSON, handling errors."""
        async with self.semaphore:
            try:
                if not self._session or self._session.closed:
                    print(f"Warning: Session is closed. Cannot fetch {url}.")
                    return None
                await asyncio.sleep(0.1) # Be nice to the APIs
                async with self._session.get(url) as response:
                    if response.status == 200:
                        return await response.json()
                    else:
                        print(f"Warning: API request failed for {url} with status {response.status}")
                        return None
            except Exception as e:
                print(f"Warning: API request error for {url}: {e}")
                return None

    async def get_python_info(self, name: str) -> Tuple[str, str]:
        """Fetch license and URL for a Python package."""
        data = await self._get(f"https://pypi.org/pypi/{name}/json")
        if data and isinstance(data.get("info"), dict):
            info = data["info"]
            license_str = info.get("license") or "Unknown"
            if not license_str or license_str == "Unknown":
                 for c in info.get("classifiers", []):
                     if c.startswith("License ::"):
                         license_str = c.split("::")[-1].strip()
                         break
            
            # FINAL FIX: Safely handle cases where 'project_urls' is null
            url = ""
            project_urls = info.get("project_urls")
            if isinstance(project_urls, dict):
                url = project_urls.get("Homepage", "")
            
            if not url:
                url = info.get("home_page", "")

            return license_str, url or ""
        return "Unknown", ""

    async def get_js_info(self, name: str) -> Tuple[str, str]:
        """Fetch license and URL for a JavaScript package."""
        data = await self._get(f"https://registry.npmjs.org/{name}")
        if data:
            license_str = data.get("license", "Unknown")
            if isinstance(license_str, dict):
                license_str = license_str.get("type", "Unknown")
            url = data.get("homepage", "")
            if not url and isinstance(data.get("repository"), dict):
                url = data["repository"].get("url", "")
                if url.startswith("git+"):
                    url = url[4:]
                if url.endswith(".git"):
                    url = url[:-4]
            return str(license_str), url
        return "Unknown", ""

    async def get_java_info(self, name: str) -> Tuple[str, str]:
        """Fetch license and URL for a Java package."""
        if ":" not in name:
            return "Unknown", ""
        group, artifact = name.split(":")
        data = await self._get(f"https://search.maven.org/solrsearch/select?q=g:\"{group}\"+AND+a:\"{artifact}\"&wt=json")

        if data and isinstance(data.get("response"), dict):
            response = data["response"]
            if response.get("numFound", 0) > 0 and isinstance(response.get("docs"), list) and response["docs"]:
                doc = response["docs"][0]
                if isinstance(doc, dict):
                    return "See URL", doc.get("homepage") or f"https://mvnrepository.com/artifact/{group}/{artifact}"
        return "Unknown", ""

    async def get_dotnet_info(self, name: str) -> Tuple[str, str]:
        """Fetch license and URL for a .NET package."""
        data = await self._get(f"https://api.nuget.org/v3/registration5-semver1/{name.lower()}/index.json")

        if not (data and isinstance(data.get("items"), list) and data["items"]):
            return "Unknown", ""

        latest_page = data["items"][-1]
        if not (isinstance(latest_page, dict) and isinstance(latest_page.get("items"), list) and latest_page["items"]):
            return "Unknown", ""

        latest_entry_summary = latest_page["items"][-1]
        if not (isinstance(latest_entry_summary, dict) and isinstance(latest_entry_summary.get("catalogEntry"), dict)):
            return "Unknown", ""

        catalog_entry_ref = latest_entry_summary["catalogEntry"]
        latest_entry_url = catalog_entry_ref.get("@id")

        if not latest_entry_url:
            return "Unknown", ""

        entry_data = await self._get(latest_entry_url)
        if entry_data:
            return entry_data.get("licenseExpression", "Unknown"), entry_data.get("projectUrl", "")

        return "Unknown", ""


# --- Core Analysis Logic ---

async def clone_repo(repo_url: str, target_dir: Path) -> bool:
    """Clones a repository using the git command line."""
    if target_dir.exists():
        return True
    target_dir.mkdir(parents=True, exist_ok=True)
    process = await asyncio.create_subprocess_exec(
        "git", "clone", "--depth=1", repo_url, str(target_dir),
        stderr=asyncio.subprocess.PIPE
    )
    _, stderr = await process.communicate()
    if process.returncode != 0:
        print(f"Error cloning {repo_url}: {stderr.decode().strip()}")
        return False
    return True

async def analyze_repository(repo_url: str, work_dir: Path, client: APIClient, dependency_map: Dict) -> RepoReport:
    """Analyzes a single repository from cloning to dependency analysis."""
    repo_name = repo_url.split('/')[-1]
    repo_path = work_dir / repo_name
    report = RepoReport(url=repo_url, name=repo_name)

    print(f"[{repo_name}] Cloning...")
    if not await clone_repo(repo_url, repo_path):
        raise RuntimeError(f"Failed to clone {repo_url}")

    print(f"[{repo_name}] Analyzing files...")
    report.license = identify_repo_license(repo_path)
    report.types = determine_repo_types(repo_path)
    report.description = extract_repo_description(repo_path)

    parsers = {
        'python': parse_python_deps,
        'javascript': parse_js_deps,
        'java': parse_java_deps,
        'dotnet': parse_dotnet_deps,
    }

    all_deps: Dict[Tuple[str, str], str] = {}
    for dep_type in report.types:
        parsed_deps = parsers[dep_type](repo_path)
        for name, version in parsed_deps.items():
            all_deps[(dep_type, name)] = version

    tasks = []
    print(f"[{repo_name}] Found {len(all_deps)} unique dependencies. Fetching info...")

    deps_by_type_count = {t: 0 for t in report.types}

    for (dep_type, name), version in all_deps.items():
        if deps_by_type_count.get(dep_type, 0) >= API_CALL_LIMIT_PER_TYPE.get(dep_type, 25):
            continue

        map_key = f"{dep_type}:{name.lower()}"
        if map_key in dependency_map:
            mapped_data = dependency_map[map_key]
            dep = Dependency(
                name=name,
                version=mapped_data.get("version", version),
                type=dep_type,
                license=f"! {mapped_data.get('license', 'Unknown')}",
                url=mapped_data.get('documentation_url', '')
            )
            report.dependencies.append(dep)
        else:
            task_map = {
                "python": client.get_python_info,
                "javascript": client.get_js_info,
                "java": client.get_java_info,
                "dotnet": client.get_dotnet_info,
            }
            if dep_type in task_map:
                tasks.append((name, version, dep_type, task_map[dep_type](name)))
                deps_by_type_count[dep_type] = deps_by_type_count.get(dep_type, 0) + 1

    results = await asyncio.gather(*(t[3] for t in tasks))
    for i, res_tuple in enumerate(results):
        try:
            name, version, dep_type, _ = tasks[i]
            if isinstance(res_tuple, tuple) and len(res_tuple) == 2:
                license_str, url = res_tuple
                report.dependencies.append(Dependency(name, version, dep_type, license_str, url))
            else:
                report.dependencies.append(Dependency(name, version, dep_type, "Lookup Failed", ""))
                print(f"Warning: Could not process result for {dep_type} dependency '{name}'. Result: {res_tuple}")
        except Exception as e:
            print(f"Error processing dependency result: {tasks[i][:3]}. Error: {e}")


    print(f"[{repo_name}] Analysis complete.")
    return report


# --- Report Generation ---

def write_csv_report(reports: List[RepoReport], filename: str):
    """Writes the final analysis to a CSV file."""
    with open(filename, 'w', newline='', encoding='utf-8') as f:
        writer = csv.writer(f)
        writer.writerow(['Repository', 'Repo License', 'Dependency', 'Dependency Type', 'Version', 'Dependency License', 'URL'])
        for report in reports:
            if not report.dependencies:
                writer.writerow([report.url, report.license, 'N/A', 'N/A', 'N/A', 'N/A', 'N/A'])
            else:
                for dep in report.dependencies:
                    writer.writerow([report.url, report.license, dep.name, dep.type, dep.version, dep.license, dep.url])
    print(f"CSV report written to {filename}")

def write_md_report(reports: List[RepoReport], filename: str):
    """Writes the final analysis to a Markdown file."""
    with open(filename, 'w', encoding='utf-8') as f:
        f.write(f"# Repository Dependency Report\n\n_Generated on {time.ctime()}_\n\n")
        f.write("_Licenses marked with `!` are from the manual `dependency_mapping.csv` file._\n\n")

        for report in reports:
            f.write(f"## [{report.name}]({report.url})\n\n")
            f.write(f"* **License**: {report.license}\n")
            f.write(f"* **Detected Types**: {', '.join(report.types) or 'None'}\n")
            f.write(f"* **Description**: {report.description}\n\n")

            if report.dependencies:
                f.write("| Dependency | Type | Version | License | Documentation |\n")
                f.write("|------------|------|---------|---------|---------------|\n")
                # Sort dependencies for consistent output
                sorted_deps = sorted(report.dependencies, key=lambda d: (d.type, d.name))
                for dep in sorted_deps:
                    url_link = f"[Link]({dep.url})" if dep.url else "N/A"
                    f.write(f"| {dep.name} | {dep.type} | {dep.version} | {dep.license} | {url_link} |\n")
                f.write("\n")
            else:
                f.write("_No dependencies found or parsed._\n\n")
            f.write("---\n\n")
    print(f"Markdown report written to {filename}")

def write_missing_mapping_report(reports: List[RepoReport], filename: str):
    """Generates a CSV for dependencies with missing information."""
    unknown_deps: Dict[Tuple[str, str], Dependency] = {}
    for report in reports:
        for dep in report.dependencies:
            is_unknown = dep.license in ["Unknown", "See URL", "", "Lookup Failed"] or not dep.url
            is_mapped = dep.license.startswith("!")
            if is_unknown and not is_mapped:
                unknown_deps[(dep.type, dep.name)] = dep

    if not unknown_deps:
        print("No dependencies with missing information found.")
        return

    with open(filename, 'w', newline='', encoding='utf-8') as f:
        writer = csv.writer(f)
        writer.writerow(['dependency_name', 'dependency_type', 'version', 'license', 'documentation_url'])
        for dep in sorted(unknown_deps.values(), key=lambda d: (d.type, d.name)):
             writer.writerow([dep.name, dep.type, dep.version, '', ''])
    print(f"Found {len(unknown_deps)} dependencies with missing info.")
    print(f"Generated a template at `{filename}`. You can fill it out and rename it to `{MAPPING_FILE}` for the next run.")


# --- Main Application ---

def load_repositories(filename: str) -> List[str]:
    """Loads repository URLs from a file."""
    if not Path(filename).exists():
        print(f"Error: `{filename}` not found.")
        print("Please create it and add one GitHub repository URL per line.")
        return []
    with open(filename, 'r') as f:
        return [line.strip() for line in f if line.strip() and not line.startswith('#')]

def load_dependency_mapping(filename: str) -> Dict:
    """Loads the manual dependency mapping file."""
    if not Path(filename).exists():
        return {}

    mapping = {}
    try:
        with open(filename, 'r', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            for row in reader:
                key = f"{row['dependency_type'].lower()}:{row['dependency_name'].lower()}"
                mapping[key] = row
        print(f"Loaded {len(mapping)} entries from `{filename}`")
    except Exception as e:
        print(f"Warning: Could not load `{filename}`: {e}")
    return mapping

def robust_rmtree(path: Path, max_retries=5, delay=2):
    """Robustly removes a directory tree, retrying on PermissionError."""
    for i in range(max_retries):
        try:
            shutil.rmtree(path)
            return
        except PermissionError:
            print(f"Warning: PermissionError removing {path}. Retrying in {delay}s ({i+1}/{max_retries})...")
            time.sleep(delay)
    print(f"Error: Failed to remove directory after {max_retries} retries.")
    print(f"Please manually delete the folder: {path.resolve()}")


async def main():
    """The main entry point for the application."""
    repos = load_repositories(REPOS_FILE)
    if not repos:
        return

    dependency_map = load_dependency_mapping(MAPPING_FILE)
    temp_dir = Path(tempfile.mkdtemp(prefix="repo_analyzer_"))
    print(f"Using temporary directory for this run: {temp_dir}")

    successful_reports = []
    failed_repos = []

    try:
        repo_semaphore = asyncio.Semaphore(MAX_CONCURRENT_REPOS)
        api_semaphore = asyncio.Semaphore(MAX_CONCURRENT_API_CALLS)

        async with APIClient(api_semaphore) as client:
            async def constrained_analyzer(repo_url: str):
                async with repo_semaphore:
                    return await analyze_repository(repo_url, temp_dir, client, dependency_map)

            tasks = [constrained_analyzer(url) for url in repos]
            results = await asyncio.gather(*tasks, return_exceptions=True)

        for i, res in enumerate(results):
            if isinstance(res, Exception):
                print(f"--- ERROR: Analysis failed for {repos[i]} ---")
                traceback.print_exception(type(res), res, res.__traceback__)
                print("-" * 50)
                failed_repos.append(repos[i])
            else:
                successful_reports.append(res)

    finally:
        if temp_dir.exists():
            robust_rmtree(temp_dir)
        print("-" * 20)

    if successful_reports:
        write_csv_report(successful_reports, CSV_REPORT_FILE)
        write_md_report(successful_reports, MD_REPORT_FILE)
        write_missing_mapping_report(successful_reports, MISSING_MAPPING_FILE)
    else:
        print("Analysis finished, but no data was successfully collected.")

    if failed_repos:
        print("\nThe following repositories failed to process:")
        for url in failed_repos:
            print(f"- {url}")

    print("Done.")


if __name__ == "__main__":
    asyncio.run(main())