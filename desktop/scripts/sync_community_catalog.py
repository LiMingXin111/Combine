"""Rebuild the shipped, commit-pinned MIT instruction catalog. Never executes upstream code."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import urllib.parse
import urllib.request

BASE = Path(__file__).resolve().parents[1] / 'runtime/community'
SOURCES = [
    ('msitarzewski/agency-agents', '4e3ac3ca6604c75113f52c695dbddcc7af928b46', [
        ('engineering/engineering-backend-architect.md', 'expert', 'Backend architecture', 'engineering'),
        ('engineering/engineering-software-architect.md', 'expert', 'Software architecture', 'engineering'),
        ('engineering/engineering-code-reviewer.md', 'expert', 'Code review', 'engineering'),
        ('engineering/engineering-data-engineer.md', 'expert', 'Data engineering', 'data'),
        ('engineering/engineering-database-optimizer.md', 'expert', 'Database optimization', 'data'),
        ('engineering/engineering-prompt-engineer.md', 'expert', 'Prompt engineering', 'AI'),
        ('engineering/engineering-technical-writer.md', 'expert', 'Technical writing', 'writing'),
        ('engineering/engineering-privacy-engineer.md', 'expert', 'Privacy review', 'engineering'),
        ('engineering/engineering-i18n-engineer.md', 'expert', 'Internationalization review', 'engineering'),
        ('design/design-ux-researcher.md', 'expert', 'UX research', 'design'),
        ('design/design-ux-architect.md', 'expert', 'UX architecture', 'design'),
        ('design/design-ui-designer.md', 'expert', 'UI design', 'design'),
        ('design/design-brand-guardian.md', 'expert', 'Brand consistency review', 'design'),
        ('design/design-visual-storyteller.md', 'expert', 'Visual storytelling', 'design'),
        ('testing/testing-accessibility-auditor.md', 'expert', 'Accessibility review', 'testing'),
        ('testing/testing-test-results-analyzer.md', 'expert', 'Test results analysis', 'testing'),
        ('testing/testing-tool-evaluator.md', 'expert', 'Tool evaluation', 'testing'),
        ('product/product-manager.md', 'expert', 'Product strategy', 'product'),
        ('product/product-feedback-synthesizer.md', 'expert', 'Feedback synthesis', 'product'),
        ('product/product-sprint-prioritizer.md', 'expert', 'Sprint prioritization', 'product'),
        ('project-management/project-manager-senior.md', 'agent', 'Project planning agent', 'planning'),
        ('project-management/project-management-experiment-tracker.md', 'agent', 'Experiment analysis agent', 'research'),
        ('project-management/project-management-meeting-notes-specialist.md', 'agent', 'Meeting notes agent', 'writing'),
        ('product/product-trend-researcher.md', 'agent', 'Trend analysis agent', 'research'),
        ('specialized/specialized-workflow-architect.md', 'agent', 'Workflow design agent', 'planning'),
        ('support/support-analytics-reporter.md', 'agent', 'Analytics report agent', 'data'),
        ('support/support-executive-summary-generator.md', 'agent', 'Executive summary agent', 'writing'),
        ('specialized/language-translator.md', 'agent', 'Translation agent', 'writing'),
    ]),
    ('github/awesome-copilot', '613efc8c21d9b3ce642dff361d17006cd9967e28', [
        ('skills/breakdown-feature-prd/SKILL.md', 'skill', 'Feature PRD', 'product'),
        ('skills/breakdown-plan/SKILL.md', 'skill', 'Task breakdown', 'planning'),
        ('skills/create-implementation-plan/SKILL.md', 'skill', 'Implementation planning', 'planning'),
        ('skills/create-architectural-decision-record/SKILL.md', 'skill', 'Architecture decision record', 'engineering'),
        ('skills/refactor-plan/SKILL.md', 'skill', 'Refactoring plan', 'engineering'),
        ('skills/sql-optimization/SKILL.md', 'skill', 'SQL optimization guidance', 'data'),
        ('skills/sql-code-review/SKILL.md', 'skill', 'SQL review', 'data'),
        ('skills/postgresql-optimization/SKILL.md', 'skill', 'PostgreSQL optimization guidance', 'data'),
        ('skills/web-design-reviewer/SKILL.md', 'skill', 'Web design review guidance', 'design'),
        ('skills/prd/SKILL.md', 'skill', 'Product requirements document', 'product'),
        ('skills/agent-architecture/SKILL.md', 'skill', 'Agent architecture guidance', 'AI'),
        ('skills/performance-review-writer/SKILL.md', 'skill', 'Performance review writing', 'writing'),
    ]),
]


def fetch(repo, commit, name):
    url = f'https://raw.githubusercontent.com/{repo}/{commit}/' + urllib.parse.quote(name, safe='/')
    request = urllib.request.Request(url, headers={'User-Agent': 'Combine-Community-Catalog/1'})
    with urllib.request.urlopen(request, timeout=30) as response:
        data = response.read(256001)
    if len(data) > 256000: raise ValueError('Source exceeds catalog limit: ' + name)
    return data


def main():
    BASE.mkdir(parents=True, exist_ok=True)
    (BASE / 'files').mkdir(exist_ok=True)
    (BASE / 'licenses').mkdir(exist_ok=True)
    entries, licenses = [], []
    for repo, commit, items in SOURCES:
        license_bytes = fetch(repo, commit, 'LICENSE')
        if 'Permission is hereby granted, free of charge' not in license_bytes.decode('utf-8-sig'):
            raise ValueError('Expected MIT license for ' + repo)
        license_name = repo.replace('/', '_') + '.txt'
        (BASE / 'licenses' / license_name).write_bytes(license_bytes)
        licenses.append({'repository': repo, 'commit': commit, 'spdx': 'MIT', 'file': 'licenses/' + license_name, 'sha256': hashlib.sha256(license_bytes).hexdigest()})
        def load(item):
            name, kind, title, category = item
            raw = fetch(repo, commit, name)
            content = raw.decode('utf-8-sig')
            if len(content) > 60000: raise ValueError('Review an explicit adapter for oversized instructions: ' + name)
            # The selected repositories were inspected for nested license files.
            # agent-architecture carries its own license; preserve and verify it.
            entry_license, entry_license_path, entry_license_data = 'licenses/' + license_name, 'LICENSE', license_bytes
            if name == 'skills/agent-architecture/SKILL.md':
                nested = fetch(repo, commit, 'skills/agent-architecture/LICENSE.txt')
                if b'Permission is hereby granted, free of charge' not in nested: raise ValueError('Unsupported nested skill license.')
                (BASE / 'licenses' / 'agent-architecture.txt').write_bytes(nested)
                entry_license, entry_license_path, entry_license_data = 'licenses/agent-architecture.txt', 'skills/agent-architecture/LICENSE.txt', nested
            key = 'community_' + hashlib.sha256((repo + ':' + name).encode()).hexdigest()[:16]
            relative = 'files/' + key + '.md'
            (BASE / relative).write_bytes(raw)
            return {'id': key, 'kind': kind, 'title': title, 'description': 'Open-source ' + kind + ' for ' + title.lower() + '. Works with supplied context.', 'category': category,
                    'repository': repo, 'commit': commit, 'path': name, 'file': relative, 'sha256': hashlib.sha256(raw).hexdigest(), 'license': 'MIT', 'license_file': entry_license, 'license_path': entry_license_path, 'license_sha256': hashlib.sha256(entry_license_data).hexdigest(),
                    'url': f'https://github.com/{repo}/blob/{commit}/{name}', 'adaptation': 'AI guidance; source tool actions are not executed.'}
        with ThreadPoolExecutor(max_workers=5) as pool:
            entries.extend(pool.map(load, items))
    manifest = {'schema': 'combine.community/v1', 'licenses': licenses, 'entries': entries}
    (BASE / 'catalog.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n', encoding='utf-8', newline='\n')
    print(json.dumps({'entries': len(entries), 'repositories': len(SOURCES), 'output': str(BASE)}))


if __name__ == '__main__': main()
