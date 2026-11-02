import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("config", ROOT / "shared" / "config.py")
config = importlib.util.module_from_spec(spec)
spec.loader.exec_module(config)


class Settings(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        config.SETTINGS_FILE = Path(self.tmp.name) / "settings.json"
        config.INSTALL_FILE = Path(self.tmp.name) / "install.json"

    def test_padrao_sem_provedores_ativos(self):
        settings = config.load_settings()
        self.assertFalse(any(config.provider_ready(n, settings) for n in config.PROVIDERS))

    def test_arquivo_privado_e_defaults_novos(self):
        settings = config.load_settings()
        settings["providers"]["subdl"].update(enabled=True, api_key="x")
        config.save_settings(settings)
        self.assertEqual(os.stat(config.SETTINGS_FILE).st_mode & 0o777, 0o600)
        again = config.load_settings()
        self.assertTrue(config.provider_ready("subdl", again))
        self.assertFalse(config.provider_ready("subsource", again))

    def test_ativo_sem_chave_nao_conta(self):
        settings = config.load_settings()
        settings["providers"]["subdl"]["enabled"] = True
        self.assertFalse(config.provider_ready("subdl", settings))

    def test_caminhos_ida_e_volta_sem_casar_prefixo_parcial(self):
        config.INSTALL_FILE.write_text(json.dumps({"libraries": [
            {"kind": "movies", "container": "/media/filmes", "db_path": "/midia/f", "host": "/midia/f"}
        ]}))
        self.assertEqual(config.to_db_path("/media/filmes/a.mkv"), "/midia/f/a.mkv")
        self.assertEqual(config.from_db_path("/midia/f/a.mkv"), "/media/filmes/a.mkv")
        self.assertIsNone(config.from_db_path("/midia/filmes-velhos/a.mkv"))


if __name__ == "__main__":
    unittest.main()
