(() => {
  "use strict";

  const samples = {
    alpaca: {
      broker: "alpaca", type: "application/json", name: "synthetic-alpaca.json",
      body: JSON.stringify([{asset_id:"904837e3-3b76-47ec-b432-046db621571b",symbol:"AAPL",asset_class:"us_equity",qty:"2",side:"long",market_value:"400",cost_basis:"300",current_price:"200",account_id:"SYNTHETIC-DEMO"}])
    },
    plaid: {
      broker: "plaid", type: "application/json", name: "synthetic-plaid.json",
      body: JSON.stringify({accounts:[{account_id:"SYNTHETIC-DEMO"}],securities:[{security_id:"synthetic-plaid-aapl",ticker_symbol:"AAPL",type:"equity",iso_currency_code:"USD"}],holdings:[{account_id:"SYNTHETIC-DEMO",security_id:"synthetic-plaid-aapl",quantity:"2",institution_price:"200",institution_value:"400",cost_basis:"300",iso_currency_code:"USD",institution_price_datetime:"2026-09-01T20:00:00Z"}]})
    },
    fidelity: {
      broker: "fidelity_csv", type: "text/csv", name: "synthetic-fidelity.csv",
      body: "Account Number,Account Name,Symbol,Description,Quantity,Last Price,Current Value,Cost Basis Total,Type\nSYNTHETIC-DEMO,Synthetic,VTI,Synthetic fund,2,100,200,150,Cash\n"
    },
    quarantine: {
      broker: "alpaca", type: "application/json", name: "synthetic-unknown-identity.json",
      body: JSON.stringify([{asset_id:"synthetic-unregistered",symbol:"AAPL",asset_class:"us_equity",qty:"2",side:"long",market_value:"400",cost_basis:"300",current_price:"200",account_id:"SYNTHETIC-DEMO"}])
    }
  };

  const section = document.querySelector(".broker-demo[data-api-url]");
  if (!section) return;
  const form = section.querySelector("[data-demo-form]");
  const status = section.querySelector("[data-demo-status]");
  const result = section.querySelector("[data-demo-result]");
  const picker = form.elements.file;
  const fileLabel = section.querySelector("[data-file-label]");
  const drop = section.querySelector("[data-demo-drop]");
  let selectedFile = null;

  const configured = section.dataset.apiUrl.trim();
  const local = location.hostname === "127.0.0.1" || location.hostname === "localhost";
  const endpoint = configured || (local ? "http://127.0.0.1:8140/normalize" : "");

  function node(tag, text, className) {
    const el = document.createElement(tag);
    if (text !== undefined) el.textContent = String(text);
    if (className) el.className = className;
    return el;
  }

  function setStatus(state, title, detail) {
    status.dataset.state = state;
    status.replaceChildren(node("strong", title), node("p", detail));
  }

  function useFile(file) {
    selectedFile = file || null;
    fileLabel.textContent = file ? file.name : "Drop one CSV or JSON file here, or choose a file";
  }

  picker.addEventListener("change", () => useFile(picker.files[0]));
  ["dragenter", "dragover"].forEach(name => drop.addEventListener(name, event => {
    event.preventDefault();
    drop.classList.add("is-dragging");
  }));
  ["dragleave", "drop"].forEach(name => drop.addEventListener(name, event => {
    event.preventDefault();
    drop.classList.remove("is-dragging");
  }));
  drop.addEventListener("drop", event => useFile(event.dataTransfer.files[0]));

  section.querySelectorAll("[data-sample]").forEach(button => button.addEventListener("click", () => {
    const sample = samples[button.dataset.sample];
    form.elements.broker.value = sample.broker;
    form.elements.account.value = "SYNTHETIC-DEMO";
    form.elements.as_of.value = "2026-09-01T20:00";
    form.elements.currency.value = "USD";
    form.elements.total_scope.value = "holdings";
    useFile(new File([sample.body], sample.name, {type: sample.type}));
    setStatus("idle", "Synthetic example loaded", "Review the explicit metadata, then normalize the portfolio.");
    result.hidden = true;
  }));

  function addSummary(root, normalization) {
    const summary = node("div", undefined, "demo-summary");
    [
      [normalization.positions?.length ?? 0, "Normalized positions"],
      [normalization.valuation_complete ? "Complete" : "Incomplete", "Valuation completeness"],
      [normalization.reconciliation || "Not provided", "Reconciliation"],
      [normalization.status || "Unknown", "Engine status"]
    ].forEach(([value, label]) => {
      const card = node("div");
      card.append(node("strong", value), node("span", label));
      summary.append(card);
    });
    root.append(summary);
  }

  function addIssues(root, items, heading) {
    if (!items || !items.length) return;
    root.append(node("h4", heading));
    const list = node("ul");
    items.forEach(item => list.append(node("li", typeof item === "string" ? item : item.code || "Unspecified issue")));
    root.append(list);
  }

  function renderAccepted(payload) {
    const normalization = payload.normalization;
    const body = node("div", undefined, "demo-result");
    addSummary(body, normalization);
    addIssues(body, normalization.issues, "Warnings and issues");
    if (normalization.positions?.length) {
      body.append(node("h4", "Positions"));
      const scroll = node("div", undefined, "table-scroll");
      const table = node("table", undefined, "demo-positions");
      const head = node("thead");
      const hr = node("tr");
      ["Instrument", "Quantity", "Value", "Currency"].forEach(label => hr.append(node("th", label)));
      head.append(hr);
      const tbody = node("tbody");
      normalization.positions.forEach(position => {
        const row = node("tr");
        const value = position.value ?? position.reported_value ?? "Unavailable";
        [position.instrument_id, position.quantity, value, position.currency].forEach(cell => row.append(node("td", cell)));
        tbody.append(row);
      });
      table.append(head, tbody);
      scroll.append(table);
      body.append(scroll);
    }
    body.append(node("p", `Trace ID: ${payload.request_id}`, "demo-trace"));
    result.replaceChildren(body);
    result.hidden = false;
    setStatus("accepted", "Accepted", "The existing deterministic engine normalized and verified this request.");
  }

  function renderRefusal(payload, fallback) {
    const reasons = Array.isArray(payload.reason_codes) ? payload.reason_codes : [];
    const body = node("div", undefined, "demo-result");
    addIssues(body, reasons, "Reason codes");
    if (payload.request_id) body.append(node("p", `Trace ID: ${payload.request_id}`, "demo-trace"));
    result.replaceChildren(body);
    result.hidden = false;
    if (payload.outcome === "quarantined") {
      setStatus("quarantined", "Quarantined for review", "The input was not safely normalizable; no portfolio was returned.");
    } else if (payload.outcome === "invalid_request") {
      setStatus("failure", "Invalid request", "Check the file type and every required metadata field.");
    } else {
      setStatus("failure", "Operational failure", fallback || "The service could not complete this request.");
    }
  }

  form.addEventListener("submit", async event => {
    event.preventDefault();
    const file = selectedFile || picker.files[0];
    if (!file) {
      setStatus("failure", "File required", "Choose one supported CSV or JSON export.");
      return;
    }
    if (!endpoint) {
      setStatus("failure", "Demo API unavailable", "The service is locally verified, but no public backend endpoint is configured. No result has been fabricated.");
      result.hidden = true;
      return;
    }
    const asOf = form.elements.as_of.value;
    const observed = asOf ? `${asOf}:00Z` : "";
    const type = form.elements.broker.value === "fidelity_csv" ? "text/csv" : "application/json";
    setStatus("idle", "Normalizing…", "The request is running inside an isolated temporary workspace.");
    result.hidden = true;
    try {
      const response = await fetch(endpoint, {
        method: "POST",
        mode: "cors",
        cache: "no-store",
        headers: {
          "Content-Type": type,
          "X-Broker": form.elements.broker.value,
          "X-Account": form.elements.account.value,
          "X-As-Of": observed,
          "X-Currency": form.elements.currency.value.toUpperCase(),
          "X-Total-Scope": form.elements.total_scope.value
        },
        body: file
      });
      const payload = await response.json();
      if (payload.outcome === "accepted" && payload.normalization) renderAccepted(payload);
      else renderRefusal(payload, `The service returned HTTP ${response.status}.`);
    } catch (_error) {
      setStatus("failure", "Demo API unavailable", "The backend could not be reached. No result has been fabricated.");
    }
  });

  if (!endpoint) {
    setStatus("failure", "Demo API not yet public", "The browser experience and backend are locally verified, but no authorized public backend target is configured.");
  }
})();
