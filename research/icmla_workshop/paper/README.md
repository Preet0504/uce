# UCE Workshop Paper (IEEE Conference Format)

## Build PDF

```powershell
cd research/icmla_workshop/paper
.\build.ps1
```

Requires **Python 3** (`matplotlib`) and **pdflatex** (MiKTeX or TeX Live). Output: `UCE_Governed_Agents_IEEE.pdf`.

Manual steps:

```powershell
python generate_figures.py
pdflatex main
bibtex main
pdflatex main
pdflatex main
```

## Template

Uses standard `IEEEtran` conference class per [IEEE conference templates](https://www.ieee.org/conferences/publishing/templates).

## Figures

| File | Content |
|------|---------|
| `fig0_architecture` | UCE pipeline |
| `fig1_agent_mcp_lift` | Prompt-only vs MCP recall/F1 |
| `fig2_enforcement_multirepo` | 4-repo catch rates |
| `fig3_context_ladder` | Context augmentation vs graph |
| `fig4_rbac_enforcement` | Hard RBAC breaches |
| `fig5_ablation_propagation` | Direct/transitive/callchain |

Data sources: `../results/agent_mcp_eval/`, `enforcement_eval/`, `context_comparison/`, `rbac_complexity/`, `ablation.json`.
