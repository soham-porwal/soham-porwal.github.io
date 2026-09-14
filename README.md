# Soham Porwal — portfolio and work samples

[View the portfolio](https://soham-porwal.github.io/) · [LinkedIn](https://www.linkedin.com/in/soham-porwal) · [Email](mailto:soham.porwal@gmail.com)

Product and data analysis, brokerage integrations, and AI evaluation. The website includes professional case studies and independent projects; the code below is independent work developed with coding agents, not employer source code.

## Browse the samples

| Sample | What to inspect | Case study | Download |
| --- | --- | --- | --- |
| [Brokerage normalization](samples/brokerage-normalization/) | Broker adapters, validation rules, SQLite transactions, replay and tests | [Brokerage data and portfolio AI](https://soham-porwal.github.io/appmosis.html) | [ZIP](https://soham-porwal.github.io/brokerage-normalization.zip) |
| [Retrieval evaluation examples](samples/evaluation-examples/) | Anonymous paired scores, metric calculations and tests | [Source diversity in AI search](https://soham-porwal.github.io/retrieval.html) | [ZIP](https://soham-porwal.github.io/work-samples.zip) |

The brokerage sample uses public vendor examples and synthetic cases. It runs offline and does not connect to customer accounts. The retrieval example recomputes a recorded result from anonymous scores; it does not include the private corpus or rerun the original search. Its package also retains an earlier, small synthetic normalization example.

## Run locally

Python 3.12 or newer. The checks use the standard library; no API keys or model are required.

```sh
git clone https://github.com/soham-porwal/soham-porwal.github.io.git
cd soham-porwal.github.io/samples/brokerage-normalization
python -B checkpoint.py
```

For the evaluation sample, from the repository root:

```sh
cd samples/evaluation-examples
python -B retrieval_eval/scorer.py
python -B verify.py
```

Each sample has its own README with inputs, expected results, limitations and test commands. The browsable sample files match the downloadable packages. Vendor attribution and licenses remain alongside the brokerage fixtures in `sources/`.

## Website

The HTML and CSS at the repository root serve the portfolio through GitHub Pages. `/v2/` preserves earlier links to the same reviewed pages. This repository contains only the public website and the selected sample files; private project repositories and employer materials are not included.
