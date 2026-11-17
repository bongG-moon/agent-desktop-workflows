from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
import posixpath
import re
import sys
from tempfile import TemporaryDirectory
import unittest
from zipfile import ZipFile


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
from build_desktop_select import build


class PackagedReferences(unittest.TestCase):
    def test_skill_references_and_guide_sections_resolve_inside_zip(self):
        with TemporaryDirectory() as folder, redirect_stdout(StringIO()):
            archive = build(Path(folder))
            with ZipFile(archive) as package:
                documents = ('desktop-select/SKILL.md', 'desktop-select/references/host-details.md')
                for document in documents:
                    markdown = package.read(document).decode('utf-8')
                    for link in re.findall(r'\]\(([^)]+)\)', markdown):
                        if link.startswith(('https://', 'http://')):
                            continue
                        with self.subTest(document=document, link=link):
                            path, _, anchor = link.partition('#')
                            target = posixpath.normpath(posixpath.join(posixpath.dirname(document), path))
                            content = package.read(target).decode('utf-8')
                            if anchor:
                                headings = re.findall(r'^#+ (.+)$', content, re.MULTILINE)
                                self.assertIn(anchor, {title.casefold().replace(' ', '-') for title in headings})
