# Software factory: provenance and license

The files under `factory/` and `.claude/skills/factory/` and the factory agents in `.claude/agents/`
(intake, spec-writer, builder, reviewer-code, reviewer-security, reviewer-ui, reviewer-ux, approver)
come from the zazencodes software factory, licensed under the GNU General Public License v3.0
(full text in `factory/LICENSE`).

- Source: https://github.com/zazencodes/zazencodes-season-3/tree/main/src/software-factory-claude-code-v2
- Commit: 94f7ae7b9dced7ffa87fb4a58312c89522ee120c

Adapted here for Windows and this repository (python instead of python3, Git Bash, checks.sh, config,
agent tailoring, jobdb wrapper), following the adaptation already in use in the SETH repository.
Keep factory code out of `backend/src` and `frontend/src`: it is GPLv3, the application is not.
