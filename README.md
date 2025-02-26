# dependency-analyser

The code analyses a list of GitHub repositories to identify their dependencies and their respective licenses.
It supports Python, JavaScript, Java, and .NET projects. The code clones each repository, analyses its 
dependency files (e.g., requirements.txt, package.json, pom.xml), and attempts to identify the license of each dependency.
It then generates a CSV report and a Markdown report with the analysis results.

