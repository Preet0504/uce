# Build IEEE PDF for UCE paper
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

Write-Host "Generating figures..."
python generate_figures.py

$miktex = "$env:LOCALAPPDATA\Programs\MiKTeX\miktex\bin\x64"
if (Test-Path $miktex) { $env:PATH = "$miktex;" + $env:PATH }
$pdflatex = Get-Command pdflatex -ErrorAction SilentlyContinue
if (-not $pdflatex) {
    Write-Host "pdflatex not found. Install MiKTeX or TeX Live, then re-run: .\build.ps1"
    Write-Host "Figures are ready in figures/. main.tex is ready to compile."
    exit 1
}

Write-Host "Compiling LaTeX..."
& pdflatex -interaction=nonstopmode main.tex | Out-Null
& bibtex main | Out-Null
& pdflatex -interaction=nonstopmode main.tex | Out-Null
& pdflatex -interaction=nonstopmode main.tex | Out-Null

if (Test-Path main.pdf) {
    Copy-Item main.pdf UCE_Governed_Agents_IEEE.pdf -Force
    Write-Host "Built: $PSScriptRoot\UCE_Governed_Agents_IEEE.pdf"
} else {
    Write-Error "PDF build failed. Check main.log"
}
