# Memory

## Me
Stuart Chen, Insider Threat SME on the Cybersecurity team. I investigate suspicious user behaviour, analyse security logs, correlate entity data, and produce evidence-based threat reports.

## Role context
- Focus area: **Insider Threat** — detecting malicious, negligent, or compromised insiders
- Current project: Automated log retrieval → normalisation → entity correlation → threat analysis → evidence preservation → report writing
- Deliverables: Threat investigation reports with evidence chains and findings

## Security tools
| Tool | Purpose |
|------|---------|
| **CrowdStrike** | EDR / endpoint telemetry |
| **Microsoft Sentinel** | Cloud SIEM, alerting, KQL queries |
| **Elastic / ELK** | Log search, dashboards, threat hunting |

## Log sources
| Source | What it covers |
|--------|---------------|
| **AD / Windows Events** | Authentication, privilege use, lateral movement |
| **DLP** | Data exfiltration attempts, policy violations |
| **Cloud storage** | SharePoint, OneDrive, S3 — file access & transfers |
| **Google Workspace** | Gmail, Drive, Admin activity logs |

## Key people
*(none added yet — tell me "remember [name] is [role]" to add)*
→ Full list: memory/glossary.md, profiles: memory/people/

## Terms & acronyms
| Term | Meaning |
|------|---------|
| IOC | Indicator of Compromise |
| TTP | Tactics, Techniques & Procedures (MITRE ATT&CK) |
| UBA / UEBA | User (and Entity) Behaviour Analytics |
| IOI | Indicator of Insider threat |
| DLP | Data Loss Prevention |
| EDR | Endpoint Detection & Response |
| SIEM | Security Information & Event Management |
| SOC | Security Operations Centre |
| IR | Incident Response |
| ToE | Time of Event |
| PoI | Person of Interest |
| CoC | Chain of Custody (evidence) |
| MITRE | MITRE ATT&CK framework |
| KQL | Kusto Query Language (Sentinel) |
| SPL | Search Processing Language (Splunk) |
→ Full glossary: memory/glossary.md

## Active projects
| Name | What |
|------|------|
| **AI workflow demo** | Automated insider threat pipeline: log ingest → normalise → correlate → analyse → evidence → report |
| **Content moderation** | Real-time Google Chat moderation: keyword + LLM text screening, Cloud Vision image violence detection, Pub/Sub listener, case/evidence creation |
→ Details: memory/projects/

## Code execution
- **Python virtual environment**: All Python scripts in this project must be executed within the project's virtual environment (`venv/`). Always activate it before running any Python code (`source venv/bin/activate`). Do not use the system Python interpreter or install packages globally.

## Preferences
- Summaries: bullet points first, then detail if needed
- Reports: detailed written format with explicit evidence chains
- Always flag ambiguity rather than assume — ask if uncertain
- Keep evidence chains traceable (source → event → finding)
- Prefer precise, factual language; avoid hedging on confirmed findings
