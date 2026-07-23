# <img src="web/static/nav_32.png" alt="pueo" height="32" valign="middle"> pueo

[![CI](https://github.com/AndysWorth/pueo/actions/workflows/test.yml/badge.svg)](https://github.com/AndysWorth/pueo/actions/workflows/test.yml)
[![codecov](https://codecov.io/gh/AndysWorth/pueo/graph/badge.svg)](https://codecov.io/gh/AndysWorth/pueo)

A vigilant, self-healing agentic AI system designed to monitor, maintain, and repair Home Assistant instances. 

`pueo` runs entirely on-device — all inference is local via Ollama, with zero cloud API calls during active monitoring or repair cycles.

---

## 🌺 Naming & Cultural Attribution

This project is named **Pueo** (the endemic Hawaiian short-eared owl, pronounced *poo-eh-oh*). 

In Hawaiian culture, the pueo is traditionally revered as an **ʻaumākua**—an ancestral guardian spirit that watches over, guides, and protects a home and its family. Furthermore, the word *pueo* historically links to the **ʻaho pueo**, the main structural cross-beams that physically hold a traditional house together.

### Why this name?
We chose this name with deep humility and respect for the Hawaiian language (`ʻōlelo Hawaiʻi`) and culture. This AI agent's architecture directly mirrors the protective, vigilant, and self-healing traits of the pueo. It serves as a persistent digital guardian, ensuring your home's automation infrastructure remains stable and resilient.

### Commitment to Non-Commercialization
In alignment with the spirit of open-source and out of respect for Native Hawaiian traditional knowledge principles, **this software is 100% free, non-commercial, and open-source**. 
* The maintainers strictly prohibit the commercialization, packaging, or corporate trademarking of this repository under the name "Pueo".
* To learn more about the biological preservation of this endangered endemic bird, please visit the [Honolulu Zoo Society Pueo Profile](https://honoluluzoo.org).

---

## 🚀 Core Features

*   **Vigilant Monitoring:** Streams live HA logs via `ha core logs --follow` over SSH and triages entries with a local AI model.
*   **Automated Diagnostics:** Fetches and analyses `configuration.yaml` for syntax errors, deprecated keys, and missing required blocks.
*   **Self-Healing Actions:** Sandbox-tests proposed fixes before writing to production; always creates a native HA backup snapshot first.
*   **Privacy-First:** All inference runs on a local Ollama instance — zero cloud API calls during active monitoring or repair cycles.

---

## 🛠️ Quick Start

### 1. Prerequisites
*   Home Assistant OS with the **Terminal & SSH** App installed (`Settings → Apps → Terminal & SSH`).
    Set `port: 22`, add your public key under `authorized_keys`, and start the App.
*   [Ollama](https://ollama.com) installed and running locally (macOS Apple Silicon recommended).
*   Python 3.14 available — either via [Homebrew](https://brew.sh) (`brew install python@3.14`) or [pyenv](https://github.com/pyenv/pyenv).

> **Passphrase-protected SSH keys:** Pueo uses `asyncssh` and cannot prompt for a passphrase interactively. Add your key to the macOS keychain once before running Pueo:
> ```bash
> ssh-add --apple-use-keychain ~/.ssh/id_ed25519
> ```
> `setup.sh` will remind you if the agent is not active or the key is not loaded.

### 2. Installation & Configuration
Clone the repository and run the setup script:
```bash
git clone https://github.com/AndysWorth/pueo
cd pueo
./setup.sh
```

`setup.sh` is idempotent — safe to re-run at any time. It will:
- Locate Python 3.14 (Homebrew or pyenv) and create a `.venv`
- Check that Ollama is installed and running, and pull `qwen2.5-coder:7b` if missing
- Generate an SSH key if none exists and show instructions for adding it to the Terminal & SSH App
- Check that the SSH agent is running and the key is loaded
- Prompt for your HA hostname, SSH settings, and agent preferences, then write `config.yaml`
- Connect to HA over SSH, detect the HA version, and warn if the log file is missing
- Run `./setup.sh --clean` to wipe all generated files and start from scratch

A reference template for `config.yaml` is available in `config.yaml.default`.

### 3. Running the Agent
```bash
source .venv/bin/activate

python main.py --mode monitor            # live SSH log tail with AI triage (default, daemon)
python main.py --mode diagnose           # one-shot config fetch and analysis
python main.py --mode advanced           # diagnose + SQLite memory + backup triggering
python main.py --mode repair             # full sandbox-test-then-atomic-swap repair cycle
python main.py --mode netalertx-setup    # install and configure NetAlertX on HA
python main.py --mode netalertx          # monitor NetAlertX logs continuously
python main.py --mode netalertx-diagnose # one-shot NetAlertX health check and optional heal
python main.py --mode dashboard          # HITL web dashboard for approving/rejecting actions
```

Pass `--config /path/to/config.yaml` if your config file is not in the project directory.

---

## 📄 License

Distributed under the **GNU Lesser General Public License v3.0 (LGPL-3.0)**. Downstream modifications must remain entirely free and open-source. Commercial corporate branding or exclusive trademark enforcement of this code under the name "Pueo" is strictly prohibited under our cultural attribution guidelines.
