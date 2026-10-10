# Landscape

Where ChipLeap sits among AI chip-design products, research and open tools, based on public material read on 10 October 2026. Claims are attributed to their source with a date. If something here is wrong or out of date, please open an issue with a link.

## Summary

- Most agentic EDA products start from a specification or existing RTL: they generate and verify RTL, debug simulations, or close implementation. Architecture exploration (deciding what to build for a workload) is claimed by a few (Siemens Fuse, Cognichip, Ricursive) but with no public product that shows it.
- Workload-to-chip companies exist (Architect Labs, Cognichip) and are closed services. Architect Labs' published Redwood work had humans fix the workload and top-level architecture in a spec, and the AI built from there.
- Academic architecture tools (Timeloop and Accelergy, MAESTRO, ZigZag, Stream, LLMCompass, GenZ) produce numbers, not hardware, and the LLM-specific ones target datacenters. A recent reinforcement-learning paper searches ASIC architectures for LLMs with all weights on chip and no external memory.
- No tool found models HyperRAM or PSRAM-class memory or asks whether a memory PHY exists at the chosen node. No published tool checks its architecture model cycle for cycle against RTL it generates. No one offers this on an open PDK.

That is the gap ChipLeap targets: workload to decisions (including external memory and process node), decisions to an X-HEEP SoC and accelerator RTL, and a model checked against that RTL, all open and local.

## Players

| Player | Starts from | What it does, per its own material | Open | Source |
|---|---|---|---|---|
| ChipAgents | Specification | Reads specs; generates RTL, UVM environments and assertions; waveform root-cause analysis | No | [Series A1 release, 17 Feb 2026](https://itweb.africa/article/chipagents-raises-74m-to-scale-an-agentic-ai-platform-to-accelerate-chip-design/nWJad7bNQOK7bjO1) |
| Cadence ChipStack AI Super Agent | Specification | Builds a model of the spec, generates test plans and tests, runs Cadence simulation and formal; an RTL generation agent in early access | No | [The Register, 10 Feb 2026](https://theregister.com/2026/02/10/cadences_agentic_chip_design_tool); [engineering.com, Sep 2026](https://www.engineering.com/cadence-adds-rtl-generation-to-chipstack-ai-super-agent/) |
| Synopsys Autopilot and AgentEngineer | Specification or RTL | Agents for verification closure, validation, implementation, analog, manufacturing; architecture is a separate product (Platform Architect) | No | [Synopsys, 28 Sep 2026](https://www.synopsys.com/ko-kr/korean/press-releases/26-09-28-synopsys-powers-autonomous-engineering.html) |
| Siemens Fuse EDA AI Agent | Architecture and planning | Orchestrates Siemens tools, "architectural exploration and design planning", MCP-connected tools | No | [Siemens, 16 Mar 2026](https://news.siemens.com/en-us/siemens-fuse-eda-ai-agent/) |
| Ricursive Intelligence | Claimed end to end | "layout, optimization, verification, and architecture exploration"; no public product | No | [Converge Digest, 3 Dec 2025](https://convergedigest.com/ricursive-intelligence-aims-to-accelerate-semiconductor-design/) |
| Cognichip | Claimed architecture onward | Site lists architecture exploration, spec and RTL generation, testbenches, PPA estimates; private beta | No | [cognichip.ai](https://www.cognichip.ai); [TechCrunch, 1 Apr 2026](https://techcrunch.com/2026/04/01/cognichip-wants-ai-to-design-the-chips-that-power-ai-and-just-raised-60m-to-try/) |
| Architect Labs | Human-written spec of workload and architecture | Redwood: AI generated the performance model, RTL, verification, firmware and kernels; Qwen3-0.6B at 12.1 tokens/s on an FPGA with LPDDR4; ASIC numbers projected | No | [arXiv:2608.26418, Aug 2026](https://arxiv.org/abs/2608.26418) |
| Chipmind | Specification or RTL | Agents write RTL, run simulation, lint and synthesis, and deliver pull requests | No | [chipmind.ai](https://www.chipmind.ai) |
| SigmanticAI | Natural language to RTL | RTL and testbench generation in an IDE, with synthesis feedback | No | [YC launch](https://www.ycombinator.com/launches/NzI-sigmanticai-the-cursor-for-hardware-design); [AnySilicon, 9 Apr 2026](https://anysilicon.com/news/sigmanticai-ai-native-rtl-and-verification-generation-platform/) |
| Hardware Intelligence | Simulation results | Waveform debugger with a verification agent | No | [hardwareintelligence.ai](https://hardwareintelligence.ai) |
| Visibl Semiconductors | Specification and RTL | Watches specs, RTL and CI for drift; also custom-silicon services for power electronics | No | [YC profile](https://www.ycombinator.com/companies/visibl-semiconductors) |
| Standard Machines | Not a design tool | Reinforcement-learning environments and sealed evaluations for chip-design agents, scored against a speed-of-light bound | No | [YC profile](https://www.ycombinator.com/companies/standard-machines) |
| Mirabilis VisualSim | Architecture | System-level discrete-event models with AI assistance | No | [mirabilisdesign.com](https://www.mirabilisdesign.com/ai-assisted-visualsim-architecture-exploration-for-semiconductors-and-systems/) |

## Research

**RL-driven ASIC architecture exploration.** R. Ganti and S. Xu, "From LLM to Silicon: RL-Driven ASIC Architecture Exploration for On-Device AI Inference", [arXiv:2604.07526](https://arxiv.org/abs/2604.07526), April 2026. It uses Soft Actor-Critic over a mesh of RISC-V tensor cores, the network-on-chip and operator placement, for Llama 3.1 8B and SmolVLM at seven nodes from 3 to 28 nm. Performance, power and area come from an analytical model. Weights sit in on-chip memory ("no HBM"), and no external memory interface or host CPU is modelled. The paper mentions RTL artifacts but evaluates none, with no synthesis and a single seed. No code was found. The authors say silicon validation is still needed.

**Explainable, bottleneck-driven design-space exploration.** This is ChipLeap's closest prior art, and its agent loop follows it:

- [Explainable-DSE](https://dl.acm.org/doi/abs/10.1145/3623278.3624772) (S. Dave, T. Nowatzki, A. Shrivastava, ASPLOS 2024) builds a bottleneck graph, maps each factor to the parameters that affect it, and applies mitigations, for edge DNN accelerators.
- [AgentDSE](https://arxiv.org/abs/2606.21836) (June 2026) and [Beacon](https://arxiv.org/abs/2608.30932) (August 2026) put LLM agents in the loop with inspectable reasoning.

None of the three closes the loop to RTL, synthesis or an SoC.

**Performance models.** Timeloop and Accelergy, MAESTRO, ZigZag, Stream, SCALE-Sim, LLMCompass, GenZ and Calculon are open and widely used. They model accelerators (or datacenter systems, for the LLM-specific ones) and produce numbers. External memory is a bandwidth figure, and none emits an SoC configuration. Gemmini with Chipyard, ESP and CFU Playground generate SoCs, but from hand-set parameters rather than a workload search.

## Open platforms and PDKs

- [X-HEEP](https://github.com/esl-epfl/x-heep) (EPFL) is a configurable RISC-V microcontroller with a Python configuration API and an accelerator interface; eight chips have been taped out on commercial nodes. ChipLeap generates its configurations and checks them with X-HEEP's own generator.
- [IHP SG13G2](https://github.com/IHP-GmbH/IHP-Open-PDK) is an open 130 nm PDK with SRAM macros up to 8192x32. ETH Zurich has taken open-flow designs on it to silicon: Croc/MLEM at 80 MHz typical, and Basilisk with HyperBus measured at up to 124 MB/s ([Hot Chips 2025 poster](https://hc2025.hotchips.org/assets/program/posters/HC2025.ETHZ.PhilippeSauter.v02.pdf)).
- No LPDDR or DDR PHY exists for an open PDK. Commercial LPDDR4 PHYs reach down to about 40 nm.

## What edge LLM hardware achieves

Single-user decode reads every weight once per token, so it tracks memory bandwidth. Measured results show it:

- **Raspberry Pi 5, Llama 3.2 1B at 4 bits:** about 11 tokens per second ([community benchmark](https://stratospherelinuxips.readthedocs.io/en/develop/immune/research_rpi_llm_performance.html)).
- **openTPU, Qwen3-0.6B at int8:** 21.6 tokens per second with 17.1 GB/s of DDR3, running at 82 to 94 % of peak bandwidth ([openTPU](https://github.com/FeSens/openTPU)).
- **Redwood on LPDDR4, Qwen3-0.6B:** 12.1 tokens per second, about 47 % of peak bandwidth ([arXiv:2608.26418](https://arxiv.org/abs/2608.26418)).

Published microcontroller deployments so far use models under about 15 million parameters. ChipLeap's arithmetic (`chipleap require`) shows why. Llama 3.2 1B at int4 needs about 6.7 GB/s for ten tokens per second and 642 MiB of storage, which no HyperRAM or PSRAM configuration provides. On an open 130 nm PDK the realistic target is a model of about 100 million parameters at a few tokens per second.

## What ChipLeap adds

| | ChipLeap |
|---|---|
| Starts from | the workload and its targets |
| Decides | memory hierarchy, external memory and channels, dataflow, array, precision, core, bus, clock, node |
| Checks feasibility | capacity, read-only memories, the address window, and whether a PHY exists at the node |
| Answers inverse questions | `chipleap require`: what a target demands |
| Explains | a decision log with hypotheses, results and rejected alternatives |
| Generates | an X-HEEP configuration or AXI4 IP for any host, accelerator RTL, register map, driver, testbench, datasheet |
| Verifies | consistency, lint, simulation against a golden model with a cycle cross-check, synthesis, place and route with DRC and LVS on IHP SG13G2, X-HEEP's checks |
| Runs | locally, free, from the command line or any MCP assistant, with replayable run logs; on open tools by default, and on Cadence, Synopsys and Siemens simulators and synthesis, locally or on a design server, when you have them |

The agentic EDA suites drive their own vendor's tools; ChipLeap drives whichever of the open and commercial tools you have, and a PDK of your own through `chipleap tech add`.

What ChipLeap does not do: generate arbitrary RTL, debug your design, or sign off for a foundry. It decides and specifies, then hands over a verified starting point that tools further down the flow can take.
