"""Testes do wizard de terminal. Rodar com: python3 -m unittest discover -s tests"""
import importlib.util
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("setup_wizard", ROOT / "setup.py")
setup = importlib.util.module_from_spec(spec)
sys.modules["setup_wizard"] = setup
spec.loader.exec_module(setup)


class LocateConfig(unittest.TestCase):
    def make(self, *paths):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        for path in paths:
            target = root / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.touch() if target.suffix else target.mkdir(exist_ok=True)
        return root

    def test_imagem_oficial(self):
        root = self.make("data/jellyfin.db", "metadata", "root/default")
        found = setup.locate_in_config(root)
        self.assertEqual(found["data"], str(root / "data"))
        self.assertEqual(found["metadata"], str(root / "metadata"))
        self.assertEqual(found["root"], str(root / "root/default"))

    def test_imagem_linuxserver(self):
        root = self.make("data/data/jellyfin.db", "data/metadata", "data/root/default")
        found = setup.locate_in_config(root)
        self.assertEqual(found["data"], str(root / "data/data"))
        self.assertEqual(found["metadata"], str(root / "data/metadata"))
        self.assertEqual(found["root"], str(root / "data/root/default"))

    def test_config_vazio(self):
        self.assertEqual(setup.locate_in_config(self.make()), {"data": None, "metadata": None, "root": None})


class Libraries(unittest.TestCase):
    def test_le_bibliotecas_do_jellyfin_e_ignora_colecoes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name, kind, link in (("Filmes", "movies", "/midia/filmes"), ("Series", "tvshows", "/midia/series"),
                                     ("Colecoes", "boxsets", "%AppDataPath%/collections")):
                (root / name).mkdir()
                (root / name / f"{kind}.collection").touch()
                (root / name / "a.mblink").write_text(link)
            self.assertEqual(setup.read_library_folders(tmp), [
                {"kind": "movies", "db_path": "/midia/filmes"},
                {"kind": "series", "db_path": "/midia/series"},
            ])

    def test_traduz_caminho_do_container_pelo_volume_mais_especifico(self):
        mounts = [{"source": "/srv", "destination": "/media"}, {"source": "/hd/filmes", "destination": "/media/movies"}]
        self.assertEqual(setup.host_path_for("/media/movies/terror", mounts), "/hd/filmes/terror")
        self.assertEqual(setup.host_path_for("/media/outros", mounts), "/srv/outros")
        self.assertIsNone(setup.host_path_for("/mediax", mounts))

    def test_numera_pastas_repetidas(self):
        libraries = setup.build_libraries([
            {"kind": "movies", "host": "/a", "db_path": "/a"},
            {"kind": "movies", "host": "/b", "db_path": "/b"},
            {"kind": "series", "host": "/c", "db_path": "/c"},
        ])
        self.assertEqual([lib["container"] for lib in libraries], ["/media/filmes", "/media/filmes-2", "/media/series"])


class Database(unittest.TestCase):
    def test_exige_tabela_do_jellyfin_atual(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIn("não consegui", setup.database_problem(tmp))
            conn = sqlite3.connect(Path(tmp) / "jellyfin.db")
            conn.execute("CREATE TABLE Outra (a)")
            conn.commit()
            self.assertIn("BaseItems", setup.database_problem(tmp))
            conn.execute("CREATE TABLE BaseItems (a)")
            conn.commit()
            conn.close()
            self.assertIsNone(setup.database_problem(tmp))


class Rendering(unittest.TestCase):
    def test_override_com_caminhos_dificeis(self):
        text = setup.render_override(
            {"data_host": "/j/data", "metadata_host": "/j/meta"},
            [{"host": '/m/Filmes [1080p] "x"', "container": "/media/filmes"}],
            125,
        )
        volumes = [line.strip()[2:] for line in text.splitlines() if line.strip().startswith('- "/')]
        self.assertIn('/m/Filmes [1080p] "x":/media/filmes', [json.loads(v) for v in volumes])
        self.assertEqual(text.count('- "125"'), 2)
        self.assertEqual(text.count("/jellyfin_metadata"), 1)

    def test_env_preserva_chaves_ao_reexecutar(self):
        first = setup.render_env({}, 1000, 1000, "8765", "./t")
        values = dict(line.split("=", 1) for line in first.strip().splitlines())
        self.assertEqual(setup.render_env(values, 1000, 1000, "9000", "./t").count(values["SETUP_TOKEN"]), 1)
        self.assertIn(values["INTERNAL_API_KEY"], setup.render_env(values, 1000, 1000, "9000", "./t"))
        self.assertNotEqual(values["SETUP_TOKEN"], values["INTERNAL_API_KEY"])


if __name__ == "__main__":
    unittest.main()
