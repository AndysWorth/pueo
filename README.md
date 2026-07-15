# 🦉 pueo

A vigilant, self-healing agentic AI system designed to monitor, maintain, and repair Home Assistant instances. 

`pueo` operates locally or via API to scan your system logs, evaluate network telemetry (via integrations like NetAlertX), and automatically execute recovery scripts when your smart home components fail.

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

*   **Vigilant Monitoring:** Continuously parses Home Assistant event buses, Docker container logs, and MQTT streams.
*   **Automated Diagnostics:** Triages device dropouts, configuration syntax errors, and network anomalies.
*   **Self-Healing Actions:** Automatically triggers integration reloads, rolls back broken HACS updates, or reboots frozen hardware bridges.
*   **Security Integration:** Pairs natively with tool protocols like NetAlertX MCP to isolate unverified network intruders.

---

## 🛠️ Quick Start

### 1. Prerequisites
*   Home Assistant Core / OS with **Advanced Mode** enabled.
*   An active LLM backend (Local Ollama instance, OpenAI API, or Anthropic API).
*   A Home Assistant Long-Lived Access Token.

### 2. Installation & Configuration
Clone the repository and run the setup script:
```bash
git clone https://github.com
cd pueo
./setup.sh
```

`setup.sh` is idempotent — safe to re-run at any time. It will:
- Install Python 3.14 via pyenv if needed (a `.python-version` file pins the version)
- Create a `.venv` and install dependencies, or recreate it if the Python version is wrong
- Check that Ollama is installed and running, and pull `qwen2.5-coder:7b` if missing
- Generate an SSH key if none exists and show instructions for adding it to Home Assistant
- Prompt for your HA hostname, SSH settings, and agent preferences, then write `config.yaml`
- Test the SSH connection to your HA host

A reference template for `config.yaml` is available in `config.yaml.default`.

### 3. Running the Agent
```bash
python main.py --config config.yaml
```

---

## 🤖 Default System Persona

`pueo` operates using a highly structured system prompt emphasizing quiet, protective humility and absolute technical accuracy. It is explicitly instructed to act as a background utility, avoiding superficial tropes.

```text
You are Pueo, the agentic guardian spirit (ʻaumākua) of this Home Assistant instance. 
Your core directives are vigilance, protection, and automatic restoration. 
Monitor the system logs day and night, maintain the structural integrity of the home configurations, and safely breathe life back into failing network nodes. 
Be precise, interventionist when errors occur, and state your diagnostic verdicts clearly before executing repairs.
```

---

## 📄 License

Distributed under the **GNU Lesser General Public License v3.0 (LGPL-3.0)**. Downstream modifications must remain entirely free and open-source. Commercial corporate branding or exclusive trademark enforcement of this code under the name "Pueo" is strictly prohibited under our cultural attribution guidelines.

