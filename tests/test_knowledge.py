import asyncio
import tempfile
import unittest
from pathlib import Path
from weyra.knowledge import MemoryStore, SkillStore


class KnowledgeTests(unittest.TestCase):
    def test_memory_scope_and_utf8(self):
        with tempfile.TemporaryDirectory() as d:
            a = MemoryStore(Path(d) / "a")
            b = MemoryStore(Path(d) / "b")
            a.create("中文", "中文知识" * 3000)
            self.assertTrue(a.search("知识"))
            self.assertFalse(b.search("知识"))
            text = asyncio.run(a.recall("知识"))
            self.assertLessEqual(len(text.encode()), 4096)
            self.assertIn("</memory>", text)
            a.delete("中文")
            self.assertFalse(a.search("知识"))

    def test_skill_snapshot_drift(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "skills" / "test"
            p.mkdir(parents=True)
            (p / "SKILL.md").write_text("---\nname: test\ndescription: 中文知识\n---\nBody")
            (p / "guide.txt").write_text("old")
            s = SkillStore(d)
            snap = s.load("test")
            self.assertTrue(s.search("知识"))
            (p / "SKILL.md").write_text("changed")
            self.assertEqual(snap.body, "Body")
            (p / "guide.txt").write_text("new")
            with self.assertRaises(ValueError):
                s.read_resource(snap, "guide.txt")
            with self.assertRaises(ValueError):
                s.read_resource(snap, "../outside")

    def test_selector_timeout(self):
        async def run():
            with tempfile.TemporaryDirectory() as d:
                store = MemoryStore(d)
                store.create("one", "hello")

                async def selector(query, records):
                    await asyncio.sleep(0.1)
                    return ["one"]

                self.assertEqual(await store.recall("hello", selector, timeout=0.001), "")

        asyncio.run(run())

    def test_catalog_pins_body(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "skills" / "one"
            p.mkdir(parents=True)
            (p / "SKILL.md").write_text("---\nname: one\n---\noriginal")
            store = SkillStore(d)
            catalog = store.capture_catalog()
            (p / "SKILL.md").write_text("---\nname: one\n---\nchanged")
            self.assertEqual(store.load("one", catalog).body, "original")

    def test_corrupt_manifest_and_symlink(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            store = MemoryStore(root)
            (store.root / "manifest.json").write_text("{broken")
            store = MemoryStore(root)
            self.assertEqual(store.list(), [])
            outside = root / "outside.md"
            outside.write_text("private")
            (store.root / "linked.md").symlink_to(outside)
            with self.assertRaises(ValueError):
                MemoryStore(root)

    def test_catalog_resources_immutable(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "skills" / "one"
            p.mkdir(parents=True)
            (p / "SKILL.md").write_text("---\nname: one\n---\noriginal")
            (p / "resource.txt").write_text("resource")
            store = SkillStore(d)
            catalog = store.capture_catalog()
            with self.assertRaises(TypeError):
                catalog["two"] = catalog["one"]
            with self.assertRaises(TypeError):
                catalog["one"].resources["resource.txt"] = "altered"

    def test_selector_untrusted_ids(self):
        async def run():
            with tempfile.TemporaryDirectory() as d:
                store = MemoryStore(d)
                store.create("one", "hello")

                async def selector(query, records):
                    return ["../../secret", {}, "one", "one"]

                result = await store.recall("hello", selector)
                self.assertEqual(result.count("<memory "), 1)

        asyncio.run(run())

    def test_duplicate_catalog_name_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ["one", "two"]:
                folder = root / "skills" / name
                folder.mkdir(parents=True)
                (folder / "SKILL.md").write_text("---\nname: shared\n---\nbody")
            with self.assertRaises(ValueError):
                SkillStore(root).capture_catalog()

    def test_resource_symlink_swap_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            folder = root / "skills" / "one"
            folder.mkdir(parents=True)
            (folder / "SKILL.md").write_text("---\nname: one\n---\nbody")
            resource = folder / "file.txt"
            resource.write_text("same")
            outside = root / "outside.txt"
            outside.write_text("same")
            store = SkillStore(root)
            snapshot = store.load("one")
            resource.unlink()
            resource.symlink_to(outside)
            with self.assertRaises(ValueError):
                store.read_resource(snapshot, "file.txt")
