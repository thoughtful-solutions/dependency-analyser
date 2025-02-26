import os
import subprocess
import json
import re
import requests
import tempfile
import shutil
import csv
import time
from pathlib import Path
import xml.etree.ElementTree as ET
from urllib.parse import urlparse

# Try to import dulwich, but make it optional
try:
    from dulwich import porcelain
    from dulwich.repo import Repo
    HAVE_DULWICH = True
except ImportError:
    HAVE_DULWICH = False
    print("Dulwich not available, falling back to git command line")


def configure_anonymous_dulwich():
    """Configure Dulwich for anonymous GitHub access."""
    if HAVE_DULWICH:
        try:
            # Set environment variables for Git credentials
            os.environ['GIT_USERNAME'] = ''
            os.environ['GIT_PASSWORD'] = ''
            
            # Simply log that we're setting environment variables
            # This should work in most cases without needing to monkey-patch classes
            print("Configured environment variables for anonymous Git access")
            
            # No more attempts to patch Dulwich internal classes
            # as they vary across versions and can cause errors
        except Exception as e:
            print(f"Warning: Error in Dulwich configuration: {e}")
            print("Will fallback to git command line for cloning if needed")


def extract_repo_description(repo_dir):
    """Extract the description from the repository's README.md file."""
    readme_paths = [
        os.path.join(repo_dir, "README.md"),
        os.path.join(repo_dir, "Readme.md"),
        os.path.join(repo_dir, "readme.md")
    ]
    
    for path in readme_paths:
        if os.path.exists(path):
            try:
                with open(path, 'r', encoding='utf-8', errors='ignore') as f:
                    content = f.read()
                    
                    # Look for "## Description" or similar sections
                    for section_header in ["## Description", "## About", "## Overview", "# Description", "# About"]:
                        match = re.search(f"{section_header}\\s*([\\s\\S]*?)(?:^#|$)", content, re.MULTILINE)
                        if match:
                            description = match.group(1).strip()
                            # Clean up the description - remove markdown formatting
                            description = re.sub(r'(\*\*|\*|__|_)', '', description)
                            # Limit length to avoid excessively long descriptions
                            if len(description) > 500:
                                description = description[:497] + "..."
                            return description
                    
                    # If no description section found, use the first paragraph
                    paragraphs = re.split(r'\n\s*\n', content)
                    if paragraphs:
                        first_para = paragraphs[0].strip()
                        if first_para and not first_para.startswith('#'):
                            first_para = re.sub(r'(\*\*|\*|__|_)', '', first_para)
                            # Limit length
                            if len(first_para) > 300:
                                first_para = first_para[:297] + "..."
                            return first_para
            except Exception as e:
                print(f"  Error reading README.md: {e}")
    
    return "No description available"


def load_dependency_mapping():
    """Load dependency mapping from a local file if exists."""
    mapping_file = "dependency_mapping.csv"
    dependency_map = {}
    
    if os.path.exists(mapping_file):
        print(f"Found dependency mapping file: {mapping_file}")
        try:
            with open(mapping_file, 'r', encoding='utf-8', newline='') as csvfile:
                reader = csv.DictReader(csvfile)
                for row in reader:
                    # Create a unique key for each dependency
                    if 'dependency_name' in row and 'dependency_type' in row:
                        key = f"{row['dependency_type']}:{row['dependency_name']}"
                        dependency_map[key] = row
                        print(f"  Loaded mapping for: {key}")
            
            print(f"Loaded {len(dependency_map)} mapped dependencies")
            
            # Debug - print all the data
            for key, value in dependency_map.items():
                print(f"  Key: {key}")
                for field_name, field_value in value.items():
                    print(f"    {field_name}: {field_value}")
                
        except Exception as e:
            print(f"Error reading dependency mapping file: {e}")
    else:
        print("No dependency mapping file found. Create 'dependency_mapping.csv' with:")
        print("dependency_name,dependency_type,version,license,documentation_url")
        print("to provide custom dependency information.")
    
    return dependency_map


def clone_repository(repo_url, target_dir):
    """Clone a GitHub repository to the target directory using dulwich or git subprocess."""
    # Make sure the parent directory exists
    os.makedirs(os.path.dirname(target_dir), exist_ok=True)
    
    # Try to convert GitHub URL to anonymous HTTPS format
    parsed_url = urlparse(repo_url)
    if parsed_url.netloc == 'github.com':
        # Format: https://github.com/username/repo.git
        anon_url = f"https://github.com{parsed_url.path}"
        if not anon_url.endswith('.git'):
            anon_url += '.git'
    else:
        anon_url = repo_url
    
    # Ensure URL is HTTPS and properly formatted
    if anon_url.startswith('git@'):
        anon_url = anon_url.replace('git@github.com:', 'https://github.com/')
    
    # For GitHub, try direct Git subprocess first as it's more reliable for anonymous access
    try:
        print(f"  Cloning with git subprocess: {anon_url}")
        subprocess.run(['git', 'clone', '--depth=1', anon_url, target_dir], 
                    check=True, capture_output=True, 
                    text=True)
        return True
    except subprocess.CalledProcessError as e:
        print(f"  Git subprocess clone failed: {e}")
        
        # If git subprocess fails and dulwich is available, try with dulwich
        if HAVE_DULWICH:
            try:
                print(f"  Trying with dulwich: {anon_url}")
                
                # Set environment variables to ensure anonymous access
                env = os.environ.copy()
                env["GIT_USERNAME"] = ""
                env["GIT_PASSWORD"] = ""
                
                porcelain.clone(
                    anon_url, 
                    target_dir, 
                    depth=1,  # Use depth=1 for faster cloning (shallow clone)
                    checkout=True,
                    env=env
                )
                return True
            except Exception as e:
                print(f"  Dulwich clone failed: {e}")
        
        # Try one last attempt with the original URL
        if anon_url != repo_url:
            try:
                print(f"  Retrying with original URL: {repo_url}")
                subprocess.run(['git', 'clone', '--depth=1', repo_url, target_dir], 
                            check=True, capture_output=True, 
                            text=True)
                return True
            except subprocess.CalledProcessError as e:
                print(f"  Final attempt failed: {e}")
        
        print(f"Error cloning {repo_url}: Could not clone repository")
        return False


def find_files(repo_dir, filename):
    """Find all instances of a file in the repository."""
    return [os.path.join(root, filename) for root, _, files in os.walk(repo_dir) if filename in files]


def extract_github_repo_from_url(url):
    """Extract GitHub repository URL from any documentation URL."""
    if not url:
        return None
    
    # Check if it's already a GitHub URL
    github_match = re.search(r'(https?://github\.com/[^/]+/[^/]+)', url)
    if github_match:
        return github_match.group(1)
    
    # Check for GitHub URLs embedded in query parameters
    param_match = re.search(r'github\.com/([^/&?]+)/([^/&?]+)', url)
    if param_match:
        org, repo = param_match.groups()
        return f"https://github.com/{org}/{repo}"
    
    return None


def check_github_repo_license(repo_url):
    """Check a GitHub repository for license information."""
    if not repo_url:
        return None
    
    # Common license file paths to check
    license_paths = [
        "LICENSE",
        "LICENSE.md",
        "LICENSE.txt",
        "license",
        "COPYING",
        "COPYING.md",
        "COPYING.txt"
    ]
    
    for path in license_paths:
        # Construct raw content URL
        content_url = f"{repo_url.rstrip('/')}/raw/main/{path}"
        content_url_master = f"{repo_url.rstrip('/')}/raw/master/{path}"
        
        # Try to fetch license content from main branch
        try:
            response = requests.get(content_url, timeout=5)
            if response.status_code == 200:
                content = response.text
                return identify_license_from_content(content)
        except Exception:
            pass
        
        # Try master branch if main fails
        try:
            response = requests.get(content_url_master, timeout=5)
            if response.status_code == 200:
                content = response.text
                return identify_license_from_content(content)
        except Exception:
            pass
    
    # Alternative: check the GitHub API for license info
    try:
        api_url = repo_url.replace("github.com", "api.github.com/repos")
        response = requests.get(api_url, timeout=5)
        if response.status_code == 200:
            repo_info = response.json()
            if "license" in repo_info and repo_info["license"] and "name" in repo_info["license"]:
                return repo_info["license"]["name"]
    except Exception:
        pass
    
    return None


def identify_license_from_content(content):
    """Identify license type from license file content."""
    if not content:
        return None
    
    # Dictionary mapping license identifiers to their names
    license_identifiers = {
        # MIT License
        "MIT License": "MIT",
        "Permission is hereby granted, free of charge,": "MIT",
        
        # Apache License
        "Apache License": "Apache",
        "Licensed under the Apache License": "Apache",
        
        # GPL
        "GNU GENERAL PUBLIC LICENSE": "GPL",
        "Version 3": "GPL-3.0",
        "Version 2": "GPL-2.0",
        
        # BSD
        "BSD License": "BSD",
        "Redistribution and use in source and binary forms": "BSD",
        
        # LGPL
        "GNU LESSER GENERAL PUBLIC LICENSE": "LGPL",
        
        # MPL
        "Mozilla Public License": "MPL",
        
        # Microsoft licenses
        "Microsoft Public License": "MS-PL",
        "Microsoft Reciprocal License": "MS-RL",
        
        # Others
        "The Unlicense": "Unlicense",
        "Creative Commons": "CC",
        "Eclipse Public License": "EPL",
        "Do What The F*ck You Want To Public License": "WTFPL",
        "ISC License": "ISC",
        "Boost Software License": "BSL",
    }
    
    # Check the content against known license patterns
    for identifier, license_name in license_identifiers.items():
        if identifier.lower() in content.lower():
            return license_name
    
    # If we've reached here, we found a license but couldn't identify it
    return "Custom License"


def extract_python_dependencies(repo_dir):
    """Extract Python dependencies from a repository."""
    dependencies = {}
    
    # Check requirements.txt files
    for req_file in find_files(repo_dir, 'requirements.txt'):
        try:
            with open(req_file, 'r', encoding='utf-8', errors='ignore') as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith('#'):
                        # Extract package name and version
                        match = re.match(r'([a-zA-Z0-9_.-]+)([=<>~!].+)?', line)
                        if match:
                            package, version = match.groups()
                            package = package.strip()
                            version = version.strip() if version else 'latest'
                            dependencies[package] = version
        except Exception as e:
            print(f"  Error reading {req_file}: {e}")
    
    # Check setup.py files
    for setup_file in find_files(repo_dir, 'setup.py'):
        try:
            with open(setup_file, 'r', encoding='utf-8', errors='ignore') as f:
                content = f.read()
                # Look for install_requires
                install_requires = re.search(r'install_requires\s*=\s*\[(.*?)\]', content, re.DOTALL)
                if install_requires:
                    for line in install_requires.group(1).split(','):
                        line = line.strip().strip('"\'')
                        if line and not line.startswith('#'):
                            match = re.match(r'([a-zA-Z0-9_.-]+)([=<>~!].+)?', line)
                            if match:
                                package, version = match.groups()
                                package = package.strip()
                                version = version.strip() if version else 'latest'
                                dependencies[package] = version
        except Exception as e:
            print(f"  Error parsing {setup_file}: {e}")
    
    # Check pyproject.toml files
    for pyproject_file in find_files(repo_dir, 'pyproject.toml'):
        try:
            with open(pyproject_file, 'r', encoding='utf-8', errors='ignore') as f:
                content = f.read()
                # Extract dependencies section (simplified approach)
                deps_section = re.search(r'dependencies\s*=\s*\[(.*?)\]', content, re.DOTALL)
                if deps_section:
                    for line in deps_section.group(1).split(','):
                        line = line.strip().strip('"\'')
                        if line and not line.startswith('#'):
                            match = re.match(r'([a-zA-Z0-9_.-]+)([=<>~!].+)?', line)
                            if match:
                                package, version = match.groups()
                                package = package.strip()
                                version = version.strip() if version else 'latest'
                                dependencies[package] = version
        except Exception as e:
            print(f"  Error parsing {pyproject_file}: {e}")
    
    return dependencies


def extract_js_dependencies(repo_dir):
    """Extract JavaScript dependencies from a repository."""
    dependencies = {}
    
    # Find all package.json files
    for package_json in find_files(repo_dir, 'package.json'):
        try:
            with open(package_json, 'r', encoding='utf-8', errors='ignore') as f:
                data = json.load(f)
                
                # Get regular dependencies
                if 'dependencies' in data:
                    dependencies.update(data['dependencies'])
                
                # Get dev dependencies
                if 'devDependencies' in data:
                    dependencies.update(data['devDependencies'])
        except Exception as e:
            print(f"  Error parsing {package_json}: {e}")
    
    return dependencies


def extract_java_dependencies(repo_dir):
    """Extract Java dependencies from a repository."""
    dependencies = {}
    imported_packages = set()
    
    # Find all Maven pom.xml files
    for pom_file in find_files(repo_dir, 'pom.xml'):
        try:
            tree = ET.parse(pom_file)
            root = tree.getroot()
            
            # Handle namespace in pom.xml
            ns = {'maven': 'http://maven.apache.org/POM/4.0.0'}
            
            # Find dependencies with and without namespace
            dep_elements = []
            dep_elements.extend(root.findall('.//dependencies/dependency', ns))
            dep_elements.extend(root.findall('.//dependencies/dependency'))
            
            # Also check dependency management section
            dep_elements.extend(root.findall('.//dependencyManagement/dependencies/dependency', ns))
            dep_elements.extend(root.findall('.//dependencyManagement/dependencies/dependency'))
            
            for dependency in dep_elements:
                group_id = None
                artifact_id = None
                version = None
                
                # Try with namespace first, then without
                for prefix in ['maven:', '']:
                    if group_id is None:
                        group_id_elem = dependency.find(f'{prefix}groupId')
                        if group_id_elem is not None:
                            group_id = group_id_elem.text
                    
                    if artifact_id is None:
                        artifact_id_elem = dependency.find(f'{prefix}artifactId')
                        if artifact_id_elem is not None:
                            artifact_id = artifact_id_elem.text
                    
                    if version is None:
                        version_elem = dependency.find(f'{prefix}version')
                        if version_elem is not None:
                            version = version_elem.text
                
                if group_id and artifact_id:
                    dep_name = f"{group_id}:{artifact_id}"
                    dependencies[dep_name] = version or 'latest'
        except Exception as e:
            print(f"  Error parsing {pom_file}: {e}")
    
    # Check Gradle build.gradle files
    for gradle_file in find_files(repo_dir, 'build.gradle'):
        try:
            with open(gradle_file, 'r', encoding='utf-8', errors='ignore') as f:
                content = f.read()
                
                # Find dependencies using regex - expanded patterns
                dep_patterns = [
                    r'(implementation|api|compile|testImplementation|testCompile)\s*[\'"]([^\'"]*)[\'"]\s*',
                    r'(implementation|api|compile|testImplementation|testCompile)\s*\([\'"]{1}([^\'"]*)[\'"]{1}\)\s*',
                    r'(implementation|api|compile|testImplementation|testCompile)\s*group\s*:\s*[\'"]{1}([^\'"]*)[\'"]{1}\s*,\s*name\s*:\s*[\'"]{1}([^\'"]*)[\'"]{1}(?:\s*,\s*version\s*:\s*[\'"]{1}([^\'"]*)[\'"]{1})?'
                ]
                
                for pattern in dep_patterns:
                    matches = re.findall(pattern, content)
                    for match in matches:
                        if len(match) == 2:  # Simple format
                            dependency = match[1]
                            
                            # Parse version if possible
                            version_match = re.search(r'([a-zA-Z0-9_.-]+):([a-zA-Z0-9_.-]+):([a-zA-Z0-9_.-]+)', dependency)
                            if version_match:
                                group, artifact, version = version_match.groups()
                                dep_name = f"{group}:{artifact}"
                                dependencies[dep_name] = version
                            else:
                                dependencies[dependency] = 'latest'
                        elif len(match) >= 3:  # Group/name format
                            group = match[1]
                            artifact = match[2]
                            version = match[3] if len(match) > 3 else 'latest'
                            dep_name = f"{group}:{artifact}"
                            dependencies[dep_name] = version
        except Exception as e:
            print(f"  Error parsing {gradle_file}: {e}")
    
    # Scan Java source files for import statements
    imported_packages = set()
    for java_file in Path(repo_dir).glob('**/*.java'):
        try:
            with open(java_file, 'r', encoding='utf-8', errors='ignore') as f:
                content = f.read()
                
                # Find import statements
                import_matches = re.findall(r'import\s+([^;]+);', content)
                for imp in import_matches:
                    # Clean up the import
                    imp = imp.strip()
                    if imp.startswith('static '):
                        imp = imp[7:]  # Remove 'static ' prefix
                    
                    # Extract package (remove class name and wildcards)
                    parts = imp.split('.')
                    if len(parts) > 1:
                        if parts[-1] == '*':
                            package = '.'.join(parts[:-1])
                        else:
                            # Check if last part starts with uppercase (likely a class)
                            if parts[-1][0].isupper():
                                package = '.'.join(parts[:-1])
                            else:
                                package = imp
                        
                        # Only keep the root package (e.g., org.springframework)
                        root_parts = package.split('.')
                        if len(root_parts) >= 2:
                            root_package = '.'.join(root_parts[:2])
                            imported_packages.add(root_package)
        except Exception as e:
            print(f"  Error analyzing imports in {java_file}: {e}")
    
    # Map common Java packages to Maven artifacts
    package_to_artifact = {
        'org.springframework': 'org.springframework:spring-core',
        'org.springframework.boot': 'org.springframework.boot:spring-boot',
        'org.springframework.cloud': 'org.springframework.cloud:spring-cloud-commons',
        'org.springframework.data': 'org.springframework.data:spring-data-commons',
        'org.springframework.security': 'org.springframework.security:spring-security-core',
        'org.springframework.web': 'org.springframework:spring-web',
        'org.apache.commons': 'org.apache.commons:commons-lang3',
        'org.apache.logging': 'org.apache.logging.log4j:log4j-core',
        'org.apache.log4j': 'log4j:log4j',
        'org.apache.tomcat': 'org.apache.tomcat:tomcat-catalina',
        'org.slf4j': 'org.slf4j:slf4j-api',
        'org.hibernate': 'org.hibernate:hibernate-core',
        'org.thymeleaf': 'org.thymeleaf:thymeleaf',
        'org.mockito': 'org.mockito:mockito-core',
        'org.junit': 'org.junit.jupiter:junit-jupiter-api',
        'org.hamcrest': 'org.hamcrest:hamcrest-core',
        'org.assertj': 'org.assertj:assertj-core',
        'org.yaml': 'org.yaml:snakeyaml',
        'com.fasterxml': 'com.fasterxml.jackson.core:jackson-core',
        'com.fasterxml.jackson': 'com.fasterxml.jackson.core:jackson-databind',
        'com.google.code': 'com.google.code.gson:gson',
        'com.google.guava': 'com.google.guava:guava',
        'com.google.inject': 'com.google.inject:guice',
        'javax.servlet': 'javax.servlet:javax.servlet-api',
        'jakarta.servlet': 'jakarta.servlet:jakarta.servlet-api',
        'jakarta.persistence': 'jakarta.persistence:jakarta.persistence-api',
        'javax.persistence': 'javax.persistence:javax.persistence-api',
        'io.netty': 'io.netty:netty-all',
        'io.micrometer': 'io.micrometer:micrometer-core',
        'io.swagger': 'io.swagger:swagger-annotations',
        'io.jsonwebtoken': 'io.jsonwebtoken:jjwt',
        'org.projectlombok': 'org.projectlombok:lombok',
        'org.jetbrains.kotlin': 'org.jetbrains.kotlin:kotlin-stdlib',
        'reactor.core': 'io.projectreactor:reactor-core'
    }
    
    # Add detected imports as dependencies if not already included
    for package in imported_packages:
        if package in package_to_artifact:
            artifact = package_to_artifact[package]
            if ':' in artifact:
                group_id, artifact_id = artifact.split(':', 1)
                dep_name = f"{group_id}:{artifact_id}"
                if dep_name not in dependencies:
                    dependencies[dep_name] = 'detected-from-import'
    
    return dependencies


def extract_dotnet_dependencies(repo_dir):
    """Extract .NET dependencies from a repository."""
    dependencies = {}
    
    # Process .csproj files (modern .NET Core, .NET 5+)
    for csproj_file in Path(repo_dir).glob('**/*.csproj'):
        try:
            with open(csproj_file, 'r', encoding='utf-8', errors='ignore') as f:
                content = f.read()
                
                # Parse XML
                try:
                    root = ET.fromstring(content)
                    
                    # Find all PackageReference elements (modern format)
                    for ref in root.findall('.//*PackageReference'):
                        package = ref.get('Include')
                        version = ref.get('Version')
                        if package:
                            dependencies[package] = version or 'latest'
                            
                    # Find all package elements in <ItemGroup><PackageReference>
                    for ref in root.findall('.//PackageReference'):
                        package = ref.get('Include')
                        version = ref.get('Version')
                        if package:
                            dependencies[package] = version or 'latest'
                
                except ET.ParseError:
                    # If XML parsing fails, try regex as fallback
                    package_refs = re.findall(r'<PackageReference\s+Include="([^"]+)"\s+Version="([^"]+)"', content)
                    for package, version in package_refs:
                        dependencies[package] = version
        except Exception as e:
            print(f"  Error reading {csproj_file}: {e}")
    
    # Process packages.config files (older .NET Framework)
    for packages_file in Path(repo_dir).glob('**/packages.config'):
        try:
            with open(packages_file, 'r', encoding='utf-8', errors='ignore') as f:
                content = f.read()
                
                try:
                    root = ET.fromstring(content)
                    
                    # Find all package elements
                    for package in root.findall('.//package'):
                        pkg_id = package.get('id')
                        version = package.get('version')
                        if pkg_id:
                            dependencies[pkg_id] = version or 'latest'
                except ET.ParseError:
                    # If XML parsing fails, try regex as fallback
                    package_refs = re.findall(r'<package\s+id="([^"]+)"\s+version="([^"]+)"', content)
                    for package, version in package_refs:
                        dependencies[package] = version
        except Exception as e:
            print(f"  Error reading {packages_file}: {e}")
    
    # Look for dependencies in .NET Core project.json (old style)
    for project_json in Path(repo_dir).glob('**/project.json'):
        try:
            with open(project_json, 'r', encoding='utf-8', errors='ignore') as f:
                data = json.load(f)
                
                # Check dependencies section
                if 'dependencies' in data and isinstance(data['dependencies'], dict):
                    for package, version in data['dependencies'].items():
                        if isinstance(version, str):
                            dependencies[package] = version
                        elif isinstance(version, dict) and 'version' in version:
                            dependencies[package] = version['version']
                        else:
                            dependencies[package] = 'latest'
        except Exception as e:
            print(f"  Error reading {project_json}: {e}")
    
    return dependencies


def identify_license(repo_dir):
    """Identify the license of a repository."""
    license_files = ['LICENSE', 'LICENSE.md', 'LICENSE.txt', 'license', 'COPYING']
    
    for license_file in license_files:
        for file_path in find_files(repo_dir, license_file):
            try:
                with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                    content = f.read()
                    
                    # Try to identify license type
                    if re.search(r'MIT\s+License', content, re.IGNORECASE) or 'Permission is hereby granted, free of charge' in content:
                        return 'MIT License'
                    elif re.search(r'Apache\s+License', content, re.IGNORECASE):
                        return 'Apache License'
                    elif re.search(r'GNU\s+GENERAL\s+PUBLIC\s+LICENSE', content, re.IGNORECASE):
                        if 'Version 3' in content:
                            return 'GPL-3.0'
                        elif 'Version 2' in content:
                            return 'GPL-2.0'
                        else:
                            return 'GPL'
                    elif re.search(r'BSD\s+License', content, re.IGNORECASE):
                        return 'BSD License'
                    else:
                        return 'Custom License'
            except Exception as e:
                print(f"  Error reading {file_path}: {e}")
    
    # Check for package.json license field
    for package_json in find_files(repo_dir, 'package.json'):
        try:
            with open(package_json, 'r', encoding='utf-8', errors='ignore') as f:
                data = json.load(f)
                if 'license' in data:
                    return data['license']
        except Exception as e:
            print(f"  Error reading {package_json}: {e}")
    
    return 'Unknown'


def fetch_dependency_license(dependency, dep_type, version='latest', dependency_map=None):
    """Fetch license information for a dependency."""
    # Check mapping file first if available
    if dependency_map:
        # Use lowercase for case-insensitive matching
        key = f"{dep_type.lower()}:{dependency.lower()}"
        print(f"    Looking for license mapping with key: {key}")
        
        if key in dependency_map:
            map_value = dependency_map[key]
            if 'license' in map_value and map_value['license']:
                license_value = f"! {map_value['license']}"
                print(f"    FOUND mapping for {key}: {license_value}")
                return license_value
    
    # Add rate limiting to avoid API issues
    time.sleep(0.5)
    
    if dep_type == 'python':
        try:
            response = requests.get(f"https://pypi.org/pypi/{dependency}/json", timeout=10)
            if response.status_code == 200:
                data = response.json()
                if 'info' in data and 'license' in data['info'] and data['info']['license']:
                    return data['info']['license']
                # Try classifiers if license field is empty
                if 'info' in data and 'classifiers' in data['info']:
                    for classifier in data['info']['classifiers']:
                        if classifier.startswith('License ::'):
                            return classifier.split('::')[-1].strip()
                
                # Check GitHub repo if available
                if 'info' in data:
                    # Try project_urls first
                    if 'project_urls' in data['info'] and data['info']['project_urls']:
                        for url_name, url in data['info']['project_urls'].items():
                            if 'github' in url.lower() or 'source' in url_name.lower():
                                github_url = extract_github_repo_from_url(url)
                                if github_url:
                                    license_from_repo = check_github_repo_license(github_url)
                                    if license_from_repo:
                                        return license_from_repo
                    
                    # Try homepage
                    if 'home_page' in data['info'] and 'github' in data['info']['home_page'].lower():
                        github_url = extract_github_repo_from_url(data['info']['home_page'])
                        if github_url:
                            license_from_repo = check_github_repo_license(github_url)
                            if license_from_repo:
                                return license_from_repo
        except Exception as e:
            print(f"  Error fetching license for Python package {dependency}: {e}")
    
    elif dep_type == 'javascript':
        try:
            response = requests.get(f"https://registry.npmjs.org/{dependency}", timeout=10)
            if response.status_code == 200:
                data = response.json()
                if 'license' in data:
                    license_data = data['license']
                    return license_data if isinstance(license_data, str) else str(license_data)
                
                # Check GitHub repo if available
                if 'repository' in data:
                    repo = data['repository']
                    if isinstance(repo, dict) and 'url' in repo:
                        github_url = extract_github_repo_from_url(repo['url'])
                        if github_url:
                            license_from_repo = check_github_repo_license(github_url)
                            if license_from_repo:
                                return license_from_repo
        except Exception as e:
            print(f"  Error fetching license for npm package {dependency}: {e}")
    
    elif dep_type == 'java':
        try:
            if ':' in dependency:
                group_id, artifact_id = dependency.split(':', 1)
                # Try Maven Central
                url = f"https://search.maven.org/solrsearch/select?q=g:{group_id}+AND+a:{artifact_id}&rows=1&wt=json"
                response = requests.get(url, timeout=10)
                if response.status_code == 200:
                    data = response.json()
                    if 'response' in data and 'docs' in data['response'] and len(data['response']['docs']) > 0:
                        doc = data['response']['docs'][0]
                        if 'license' in doc:
                            return doc['license']
                        
                        # Check for GitHub URL in the POM
                        if 'scm' in doc and 'url' in doc['scm']:
                            github_url = extract_github_repo_from_url(doc['scm'])
                            if github_url:
                                license_from_repo = check_github_repo_license(github_url)
                                if license_from_repo:
                                    return license_from_repo
        except Exception as e:
            print(f"  Error fetching license for Java package {dependency}: {e}")
    
    elif dep_type == 'dotnet':
        try:
            # Get the documentation URL first, as we'll use it to check for GitHub repos
            doc_url = fetch_dependency_url(dependency, dep_type, version, dependency_map)
            github_url = extract_github_repo_from_url(doc_url)
            
            # If we found a GitHub repo, check for license in the repo
            if github_url:
                license_from_repo = check_github_repo_license(github_url)
                if license_from_repo:
                    return license_from_repo
            
            # If no GitHub license, try more NuGet APIs
            # Try catalog API first (more detailed)
            catalog_url = f"https://api.nuget.org/v3/registration5-semver1/{dependency.lower()}/index.json"
            response = requests.get(catalog_url, timeout=10)
            
            if response.status_code == 200:
                data = response.json()
                if 'items' in data and len(data['items']) > 0:
                    if 'items' in data['items'][0] and len(data['items'][0]['items']) > 0:
                        item = data['items'][0]['items'][0]
                        if 'catalogEntry' in item:
                            entry = item['catalogEntry']
                            
                            # Check for license directly
                            if 'license' in entry:
                                return entry['license']
                            
                            # Check license expressions
                            if 'licenseExpression' in entry:
                                return entry['licenseExpression']
                            
                            # Check license URL and try to determine type
                            if 'licenseUrl' in entry and entry['licenseUrl']:
                                license_url = entry['licenseUrl']
                                
                                # Try to determine license from URL if available
                                if 'mit' in license_url.lower():
                                    return 'MIT'
                                elif 'apache' in license_url.lower():
                                    return 'Apache'
                                elif 'gpl' in license_url.lower():
                                    return 'GPL'
                                elif 'bsd' in license_url.lower():
                                    return 'BSD'
                                elif 'ms-pl' in license_url.lower() or 'microsoft' in license_url.lower():
                                    return 'MS-PL'
                                
                                # If it's a URL to raw license file, try to fetch its content
                                if 'raw.githubusercontent.com' in license_url or 'github.com' in license_url and '/blob/' in license_url:
                                    try:
                                        # Convert blob URL to raw URL if needed
                                        if '/blob/' in license_url:
                                            license_url = license_url.replace('/blob/', '/raw/')
                                        
                                        license_response = requests.get(license_url, timeout=5)
                                        if license_response.status_code == 200:
                                            return identify_license_from_content(license_response.text)
                                    except Exception:
                                        pass
            
            # Try packageinfo API as fallback (for specific versions)
            try:
                version_url = f"https://api.nuget.org/v3-flatcontainer/{dependency.lower()}/{version}/packageinfo.json"
                response = requests.get(version_url, timeout=10)
                if response.status_code == 200:
                    data = response.json()
                    
                    # Check for license directly
                    if 'license' in data:
                        return data['license']
                    
                    # Check license expressions
                    if 'licenseExpression' in data:
                        return data['licenseExpression']
                    
                    # Check license URL
                    if 'licenseUrl' in data and data['licenseUrl']:
                        license_url = data['licenseUrl']
                        
                        # Try to determine license from URL if available
                        if 'mit' in license_url.lower():
                            return 'MIT'
                        elif 'apache' in license_url.lower():
                            return 'Apache'
                        elif 'gpl' in license_url.lower():
                            return 'GPL'
                        elif 'bsd' in license_url.lower():
                            return 'BSD'
                        elif 'ms-pl' in license_url.lower() or 'microsoft' in license_url.lower():
                            return 'MS-PL'
            except Exception:
                pass
        except Exception as e:
            print(f"  Error fetching license for .NET package {dependency}: {e}")
    
    return 'Unknown'


def fetch_dependency_url(dependency, dep_type, version='latest', dependency_map=None):
    """Fetch documentation or source code URL for a dependency."""
    # Check mapping file first if available
    if dependency_map:
        # Use lowercase for case-insensitive matching
        key = f"{dep_type.lower()}:{dependency.lower()}"
        print(f"    Looking for URL mapping with key: {key}")
        
        if key in dependency_map:
            map_value = dependency_map[key]
            if 'documentation_url' in map_value and map_value['documentation_url']:
                url_value = map_value['documentation_url']
                print(f"    FOUND mapping for {key}: {url_value}")
                return url_value
    
    # Add rate limiting to avoid API issues
    time.sleep(0.5)
    
    if dep_type == 'python':
        try:
            # Try PyPI for documentation URL
            response = requests.get(f"https://pypi.org/pypi/{dependency}/json", timeout=10)
            if response.status_code == 200:
                data = response.json()
                urls = []
                
                # Project URL
                if 'info' in data and 'project_url' in data['info'] and data['info']['project_url']:
                    urls.append(data['info']['project_url'])
                
                # Project URLs dictionary (more reliable)
                if 'info' in data and 'project_urls' in data['info'] and data['info']['project_urls']:
                    # Try to find documentation URL first
                    for label, url in data['info']['project_urls'].items():
                        if 'doc' in label.lower() or 'documentation' in label.lower():
                            urls.insert(0, url)  # Insert at the beginning to prioritize
                
                # Homepage
                if 'info' in data and 'home_page' in data['info'] and data['info']['home_page']:
                    urls.append(data['info']['home_page'])
                
                # Documentation URL
                if 'info' in data and 'docs_url' in data['info'] and data['info']['docs_url']:
                    urls.append(data['info']['docs_url'])
                
                # Package URL
                if 'info' in data and 'package_url' in data['info'] and data['info']['package_url']:
                    urls.append(data['info']['package_url'])
                
                if not urls:
                    # Default URL if nothing else is available
                    urls.append(f"https://pypi.org/project/{dependency}/")
                
                return urls[0]  # Return the first available URL
        except Exception as e:
            print(f"  Error fetching URL for Python package {dependency}: {e}")
        
        # Fallback
        return f"https://pypi.org/project/{dependency}/"
    
    elif dep_type == 'javascript':
        try:
            # Try NPM for documentation URL
            response = requests.get(f"https://registry.npmjs.org/{dependency}", timeout=10)
            if response.status_code == 200:
                data = response.json()
                urls = []
                
                # Homepage
                if 'homepage' in data and data['homepage']:
                    urls.append(data['homepage'])
                
                # Repository
                if 'repository' in data:
                    repo = data['repository']
                    if isinstance(repo, dict) and 'url' in repo:
                        # Clean up Git URLs
                        url = repo['url']
                        url = re.sub(r'^git\+', '', url)
                        url = re.sub(r'\.git$', '', url)
                        url = re.sub(r'^git:', 'https:', url)
                        urls.append(url)
                    elif isinstance(repo, str):
                        urls.append(repo)
                
                # NPM URL
                urls.append(f"https://www.npmjs.com/package/{dependency}")
                
                return urls[0]  # Return the first available URL
        except Exception as e:
            print(f"  Error fetching URL for npm package {dependency}: {e}")
        
        # Fallback
        return f"https://www.npmjs.com/package/{dependency}"
    
    elif dep_type == 'java':
        try:
            if ':' in dependency:
                group_id, artifact_id = dependency.split(':', 1)
                # Try Maven Central
                url = f"https://search.maven.org/solrsearch/select?q=g:{group_id}+AND+a:{artifact_id}&rows=1&wt=json"
                response = requests.get(url, timeout=10)
                if response.status_code == 200:
                    data = response.json()
                    if 'response' in data and 'docs' in data['response'] and len(data['response']['docs']) > 0:
                        doc = data['response']['docs'][0]
                        # Try different possible URLs
                        for url_field in ['homepage', 'url', 'download_url']:
                            if url_field in doc and doc[url_field]:
                                return doc[url_field]
                
                # Fallbacks
                urls = [
                    f"https://mvnrepository.com/artifact/{group_id}/{artifact_id}",
                    f"https://search.maven.org/artifact/{group_id}/{artifact_id}"
                ]
                return urls[0]
            else:
                return f"https://mvnrepository.com/search?q={dependency}"
        except Exception as e:
            print(f"  Error fetching URL for Java package {dependency}: {e}")
        
        # Fallback
        return f"https://mvnrepository.com/search?q={dependency}"
    
    elif dep_type == 'dotnet':
        try:
            # Try NuGet for documentation URL
            nuget_url = f"https://api.nuget.org/v3-flatcontainer/{dependency.lower()}/{version}/packageinfo.json"
            response = requests.get(nuget_url, timeout=10)
            
            if response.status_code == 200:
                data = response.json()
                urls = []
                
                # Project URL
                if 'projectUrl' in data and data['projectUrl']:
                    urls.append(data['projectUrl'])
                
                # Repository URL
                if 'repositoryUrl' in data and data['repositoryUrl']:
                    urls.append(data['repositoryUrl'])
                
                # License URL
                if 'licenseUrl' in data and data['licenseUrl']:
                    urls.append(data['licenseUrl'])
                
                if urls:
                    return urls[0]
            
            # If the specific version package info doesn't work, try the catalog
            catalog_url = f"https://api.nuget.org/v3/registration5-semver1/{dependency.lower()}/index.json"
            response = requests.get(catalog_url, timeout=10)
            
            if response.status_code == 200:
                data = response.json()
                if 'items' in data and len(data['items']) > 0:
                    if 'items' in data['items'][0] and len(data['items'][0]['items']) > 0:
                        item = data['items'][0]['items'][0]
                        if 'catalogEntry' in item:
                            entry = item['catalogEntry']
                            urls = []
                            
                            # Project URL
                            if 'projectUrl' in entry and entry['projectUrl']:
                                urls.append(entry['projectUrl'])
                            
                            # Repository URL
                            if 'repository' in entry and 'url' in entry['repository']:
                                urls.append(entry['repository']['url'])
                            
                            if urls:
                                return urls[0]
        except Exception as e:
            print(f"  Error fetching URL for .NET package {dependency}: {e}")
        
        # Fallback
        return f"https://www.nuget.org/packages/{dependency}"
    
    # Default fallback for unknown dependency types
    return f"https://www.google.com/search?q={dependency}+{dep_type}+package+documentation"


def determine_repo_types(repo_dir):
    """Determine the programming languages used in a repository."""
    repo_types = set()
    
    # Check for Python files and dependency files
    python_files = list(Path(repo_dir).glob('**/*.py'))
    python_dep_files = find_files(repo_dir, 'requirements.txt') + find_files(repo_dir, 'setup.py')
    
    if python_files or python_dep_files:
        repo_types.add('python')
    
    # Check for JavaScript/TypeScript files and package.json
    js_files = list(Path(repo_dir).glob('**/*.js')) + list(Path(repo_dir).glob('**/*.jsx')) + \
               list(Path(repo_dir).glob('**/*.ts')) + list(Path(repo_dir).glob('**/*.tsx'))
    js_dep_files = find_files(repo_dir, 'package.json')
    
    if js_files or js_dep_files:
        repo_types.add('javascript')
    
    # Check for Java files and build files
    java_files = list(Path(repo_dir).glob('**/*.java'))
    java_dep_files = find_files(repo_dir, 'pom.xml') + find_files(repo_dir, 'build.gradle')
    
    if java_files or java_dep_files:
        repo_types.add('java')
    
    # Check for .NET files and build files
    cs_files = list(Path(repo_dir).glob('**/*.cs'))
    csproj_files = list(Path(repo_dir).glob('**/*.csproj'))
    sln_files = list(Path(repo_dir).glob('**/*.sln'))
    packages_config_files = list(Path(repo_dir).glob('**/packages.config'))
    
    if cs_files or csproj_files or sln_files or packages_config_files:
        repo_types.add('dotnet')
    
    return list(repo_types)


def process_repositories(repositories):
    """Process a list of GitHub repositories and analyze their dependencies."""
    results = []
    temp_dir = tempfile.mkdtemp()
    
    # Load dependency mapping if exists
    dependency_map = load_dependency_mapping()
    normalized_map = {}
    
    # Convert all keys to lowercase for case-insensitive matching
    if dependency_map:
        for key, value in dependency_map.items():
            normalized_key = key.lower()
            normalized_map[normalized_key] = value
            print(f"  Normalized mapping: {key} -> {normalized_key}")
        
        # Replace original with normalized version
        dependency_map = normalized_map
        
        print("Available mappings:")
        for key in dependency_map.keys():
            print(f"  - {key}")
    
    try:
        for repo_url in repositories:
            print(f"Processing {repo_url}...")
            repo_name = os.path.basename(repo_url)
            repo_dir = os.path.join(temp_dir, repo_name)
            
            # Clone the repository
            if clone_repository(repo_url, repo_dir):
                # Extract repository description from README.md
                repo_description = extract_repo_description(repo_dir)
                print(f"  Repository description: {repo_description[:100]}...")
                
                # Determine repository types
                repo_types = determine_repo_types(repo_dir)
                print(f"  Detected types: {', '.join(repo_types) if repo_types else 'None'}")
                
                # Extract dependencies based on repository type
                dependencies = {}
                if 'python' in repo_types:
                    python_deps = extract_python_dependencies(repo_dir)
                    dependencies['python'] = python_deps
                    print(f"  Found {len(python_deps)} Python dependencies")
                
                if 'javascript' in repo_types:
                    js_deps = extract_js_dependencies(repo_dir)
                    dependencies['javascript'] = js_deps
                    print(f"  Found {len(js_deps)} JavaScript dependencies")
                
                if 'java' in repo_types:
                    java_deps = extract_java_dependencies(repo_dir)
                    dependencies['java'] = java_deps
                    print(f"  Found {len(java_deps)} Java dependencies")
                
                if 'dotnet' in repo_types:
                    dotnet_deps = extract_dotnet_dependencies(repo_dir)
                    dependencies['dotnet'] = dotnet_deps
                    print(f"  Found {len(dotnet_deps)} .NET dependencies")
                
                # Identify repo license
                license_info = identify_license(repo_dir)
                print(f"  Repository license: {license_info}")
                
                # Get license info and URLs for dependencies
                dependency_licenses = {}
                dependency_urls = {}
                for dep_type, deps in dependencies.items():
                    print(f"  Fetching license and URL info for {dep_type} dependencies...")
                    count = 0
                    for dep, version in deps.items():
                        # Limit to avoid too many API calls
                        if count >= 10:  # You can adjust this limit
                            print(f"  Limiting license/URL checks for {dep_type} dependencies to 10")
                            break
                        
                        license_key = f"{dep_type}:{dep}"
                        dependency_licenses[license_key] = fetch_dependency_license(dep, dep_type, version, dependency_map)
                        dependency_urls[license_key] = fetch_dependency_url(dep, dep_type, version, dependency_map)
                        count += 1
                
                results.append({
                    'repository': repo_url,
                    'types': repo_types,
                    'license': license_info,
                    'description': repo_description,
                    'dependencies': dependencies,
                    'dependency_licenses': dependency_licenses,
                    'dependency_urls': dependency_urls
                })
    finally:
        # Clean up temp directory with error handling for Windows
        try:
            # Close any open repo objects to release file handles
            if HAVE_DULWICH:
                for root, dirs, files in os.walk(temp_dir):
                    # Look for .git directories which might contain open file handles
                    if '.git' in dirs:
                        git_dir = os.path.join(root, '.git')
                        try:
                            # Try to close repo if it's a valid repository
                            if os.path.isdir(git_dir):
                                try:
                                    repo = Repo(git_dir)
                                    # Close any file descriptors
                                    for obj in getattr(repo, '_open_files', []):
                                        try:
                                            obj.close()
                                        except Exception:
                                            pass
                                except Exception:
                                    pass
                        except Exception:
                            pass
            
            # Give a moment for resources to be released
            time.sleep(0.5)
            
            # On Windows, we might need multiple attempts due to file locks
            for _ in range(3):
                try:
                    shutil.rmtree(temp_dir)
                    break
                except (PermissionError, OSError) as e:
                    print(f"Warning: Cleanup attempt failed: {e}")
                    # Give processes time to release locks
                    time.sleep(1)
            else:
                print(f"Warning: Could not fully remove temporary directory {temp_dir}")
                # If we can't remove it, at least notify the user
                print("You may need to manually delete this directory later")
        except Exception as e:
            print(f"Error during cleanup: {e}")
            # Still return results even if cleanup fails
            print(f"Temporary files may remain in {temp_dir}")
    
    return results


def write_results_to_csv(results, output_file):
    """Write analysis results to a CSV file."""
    # Reload mapping file to ensure we have the latest data
    dependency_map = load_dependency_mapping()
    normalized_map = {}
    
    # Normalize keys for case-insensitive lookup
    for key, value in dependency_map.items():
        if ':' in key:
            dep_type, dep_name = key.split(':', 1)
            normalized_key = f"{dep_type.lower()}:{dep_name.lower()}"
            normalized_map[normalized_key] = value
    
    with open(output_file, 'w', newline='', encoding='utf-8') as csvfile:
        fieldnames = ['Repository', 'Types', 'License', 'Dependency', 'Dependency Type', 'Dependency Version', 
                      'Dependency License', 'Documentation URL']
        writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
        
        writer.writeheader()
        
        for repo_result in results:
            repo_url = repo_result['repository']
            repo_types = ', '.join(repo_result['types'])
            repo_license = repo_result['license']
            
            for dep_type, deps in repo_result['dependencies'].items():
                for dep, version in deps.items():
                    dep_key = f"{dep_type}:{dep}"
                    
                    # Get license and URL from results or mapping
                    dep_license = repo_result['dependency_licenses'].get(dep_key, 'Unknown')
                    dep_url = repo_result['dependency_urls'].get(dep_key, '')
                    
                    # Direct check against mapping file as a final override
                    norm_key = f"{dep_type.lower()}:{dep.lower()}"
                    if norm_key in normalized_map:
                        mapping = normalized_map[norm_key]
                        # Override license if present in mapping
                        if mapping.get('license'):
                            dep_license = f"! {mapping['license']}"
                        # Override URL if present in mapping
                        if mapping.get('documentation_url'):
                            dep_url = mapping['documentation_url']
                    
                    writer.writerow({
                        'Repository': repo_url,
                        'Types': repo_types,
                        'License': repo_license,
                        'Dependency': dep,
                        'Dependency Type': dep_type,
                        'Dependency Version': version,
                        'Dependency License': dep_license,
                        'Documentation URL': dep_url
                    })


def generate_markdown_report(results, output_file):
    """Generate a Markdown report with analysis results."""
    # Reload mapping file to ensure we have the latest data
    dependency_map = load_dependency_mapping()
    normalized_map = {}
    
    # Normalize keys for case-insensitive lookup
    for key, value in dependency_map.items():
        if ':' in key:
            dep_type, dep_name = key.split(':', 1)
            normalized_key = f"{dep_type.lower()}:{dep_name.lower()}"
            normalized_map[normalized_key] = value
    
    with open(output_file, 'w', encoding='utf-8') as mdfile:
        mdfile.write("# Repository Dependency Analysis Report\n\n")
        mdfile.write(f"Generated on: {time.strftime('%Y-%m-%d %H:%M:%S')}\n\n")
        mdfile.write("**Note**: Licenses marked with '!' indicate manually mapped dependencies.\n\n")
        
        for repo_result in results:
            repo_url = repo_result['repository']
            repo_name = os.path.basename(repo_url)
            repo_types = ', '.join(repo_result['types'])
            repo_license = repo_result['license']
            repo_description = repo_result.get('description', 'No description available')
            
            mdfile.write(f"## {repo_name}\n\n")
            mdfile.write(f"- **Repository URL**: {repo_url}\n")
            mdfile.write(f"- **Types**: {repo_types}\n")
            mdfile.write(f"- **License**: {repo_license}\n")
            mdfile.write(f"- **Description**: {repo_description}\n")
            mdfile.write("- **Note**: Dependencies marked with '!' are from manual mapping\n\n")
            
            if repo_result['dependencies']:
                mdfile.write("### Dependencies\n\n")
                
                for dep_type, deps in repo_result['dependencies'].items():
                    if deps:
                        mdfile.write(f"#### {dep_type.capitalize()} Dependencies\n\n")
                        mdfile.write("| Dependency | Version | License | Documentation |\n")
                        mdfile.write("|------------|---------|---------|---------------|\n")
                        
                        for dep, version in deps.items():
                            dep_key = f"{dep_type}:{dep}"
                            
                            # Get license and URL from results
                            dep_license = repo_result['dependency_licenses'].get(dep_key, 'Unknown')
                            dep_url = repo_result['dependency_urls'].get(dep_key, '')
                            
                            # Direct check against mapping file as a final override
                            norm_key = f"{dep_type.lower()}:{dep.lower()}"
                            if norm_key in normalized_map:
                                mapping = normalized_map[norm_key]
                                # Override license if present in mapping
                                if mapping.get('license'):
                                    dep_license = f"! {mapping['license']}"
                                # Override URL if present in mapping
                                if mapping.get('documentation_url'):
                                    dep_url = mapping['documentation_url']
                            
                            doc_link = f"[Documentation]({dep_url})" if dep_url else 'N/A'
                            mdfile.write(f"| {dep} | {version} | {dep_license} | {doc_link} |\n")
                        
                        mdfile.write("\n")
            else:
                mdfile.write("No dependencies found.\n\n")
            
            mdfile.write("---\n\n")


def generate_missing_dependency_mapping(results, output_file):
    """Generate a CSV file listing dependencies with unknown licenses or documentation.
    
    This file can be copied to dependency_mapping.csv, edited, and used for the next run.
    """
    # Track unique dependencies with unknown info
    unknown_deps = {}
    
    for repo_result in results:
        for dep_type, deps in repo_result['dependencies'].items():
            for dep, version in deps.items():
                dep_key = f"{dep_type}:{dep}"
                
                # Check if license or documentation is unknown/missing
                dep_license = repo_result['dependency_licenses'].get(dep_key, 'Unknown')
                dep_url = repo_result['dependency_urls'].get(dep_key, '')
                
                if dep_license == 'Unknown' or not dep_url or dep_url.startswith('https://www.google.com/search'):
                    # If not already in our unknown list, add it
                    if dep_key not in unknown_deps:
                        unknown_deps[dep_key] = {
                            'dependency_name': dep,
                            'dependency_type': dep_type,
                            'version': version,
                            'license': '' if dep_license == 'Unknown' else dep_license,
                            'documentation_url': '' if dep_url.startswith('https://www.google.com/search') else dep_url
                        }
    
    # Write the CSV file
    if unknown_deps:
        with open(output_file, 'w', newline='', encoding='utf-8') as csvfile:
            fieldnames = ['dependency_name', 'dependency_type', 'version', 'license', 'documentation_url']
            writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
            
            writer.writeheader()
            for dep_info in unknown_deps.values():
                writer.writerow(dep_info)
        
        print(f"Found {len(unknown_deps)} dependencies with missing information.")
        print(f"Generated {output_file} - you can edit this file, add license and documentation info,")
        print(f"and rename it to dependency_mapping.csv for the next run.")
    else:
        print("No dependencies with missing information found.")


def main():
    """Main function to run the repository analysis."""
    # Configure Dulwich for anonymous access
    if HAVE_DULWICH:
        configure_anonymous_dulwich()
    
    repositories = [
        "https://github.com/SelectIDLtd/AuditServer",
        "https://github.com/SelectIDLtd/core-inf",
        "https://github.com/SelectIDLtd/TrustServer",
        "https://github.com/SelectIDLtd/rp-sdk-dotnet",
        "https://github.com/SelectIDLtd/rp-example-nodejs-azure",
        "https://github.com/SelectIDLtd/react-idp-selector",
        "https://github.com/SelectIDLtd/rp-test-tool",
        "https://github.com/SelectIDLtd/bff-example",
        "https://github.com/SelectIDLtd/refidp",
        "https://github.com/SelectIDLtd/id_token_generator",
        "https://github.com/SelectIDLtd/rp-sdk-java",
        "https://github.com/SelectIDLtd/rp-example-java",
        "https://github.com/SelectIDLtd/rp-example-nodejs-aws",
        "https://github.com/SelectIDLtd/rp-sdk-python",
        "https://github.com/SelectIDLtd/AuditReporting"
    ]
    
    print(f"Starting analysis of {len(repositories)} repositories...")
    results = process_repositories(repositories)
    
    # Generate CSV report
    csv_output = "dependency_report.csv"
    write_results_to_csv(results, csv_output)
    print(f"CSV report written to {csv_output}")
    
    # Generate Markdown report
    md_output = "dependency_report.md"
    generate_markdown_report(results, md_output)
    print(f"Markdown report written to {md_output}")
    
    # Generate missing dependency mapping file
    missing_mapping_output = "missing-dependency-mapping.csv"
    generate_missing_dependency_mapping(results, missing_mapping_output)


if __name__ == "__main__":
    main()
