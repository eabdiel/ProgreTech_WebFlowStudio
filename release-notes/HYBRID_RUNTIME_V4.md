# Hybrid Runtime v4

## Execution reliability
- Validate locator identity against the recorded DOM target before interacting.
- Prefer semantic link/button locators and skip selectors that resolve to a different element.
- Closing the controlled Chromium browser finalizes the run and cleans up only execution-owned Chromium processes.

## Recording-first authoring
- Designer lists real recordings by recording name, not legacy placeholder flows.
- A recording is automatically imported into Designer on first open.
- Legacy prototype flows are hidden unless real recordings/designs were attached.

## Repository organization
- Objects can be grouped by source recording or a custom group label.
- Custom group labels persist across repository rebuilds.

## Guidance
- Contextual ? help added to Designer, Object Repository, Data & Variables and Execution Console.
- Python node help now explains the governed variables/outputs contract with a concrete example.
