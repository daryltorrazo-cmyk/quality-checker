"use strict";
const $ = (id) => document.getElementById(id);
const BUREAUS = { TU: "TransUnion", EX: "Experian", EQ: "Equifax" };
const LABELS = {
  total: "Inquiries checked",
  protected: "Protected matches",
  related_hold: "Related · on hold",
  manual_review: "Manual investigation",
  unmatched: "Authorization review",
  qa_candidates: "QA verified candidates",
  duplicate_inquiries: "Duplicate inquiries",
};
const CHECKS = {
  all_accounts: "Compared against all open/positive accounts",
  affiliations: "Checked aliases, issuers, partners and subsidiaries",
  auto_financing: "Investigated dealership / lender relationships",
  dates: "Verified inquiry and account opening dates",
  application_connection: "Determined application or account connection",
  authorization: "Reviewed authorization / permissible purpose",
  factual_basis: "Documented a specific factual basis",
  qa_approval: "QA evidence and decision verified",
};
let screening = null,
  letterCheck = null,
  sourceDoc = null,
  letterDoc = null,
  intakeDoc = null,
  letterVerified = false,
  session = null,
  selectedInquiry = null,
  activeBureau = "all",
  generation = 0;
function node(tag, text, cls) {
  const el = document.createElement(tag);
  if (text !== undefined) el.textContent = text;
  if (cls) el.className = cls;
  return el;
}
function notice(text, success = false) {
  $("notice").hidden = !text;
  $("notice").textContent = text;
  $("notice").classList.toggle("success", success);
}
function scrollToId(id) {
  $(id).scrollIntoView({
    behavior: matchMedia("(prefers-reduced-motion: reduce)").matches
      ? "auto"
      : "smooth",
    block: "start",
  });
}
function invalidate(
  message = "Documents or evidence changed. Run a fresh cross-check.",
) {
  generation++;
  letterCheck = null;
  $("confirm-letter").checked = false;
  setBanner(
    message,
    "No letter can be approved until the current documents have been checked.",
  );
  $("approve-letter").disabled = true;
  $("approve-top").disabled = true;
  $("letter-results").textContent = "Cross-check required.";
}
async function api(path, body) {
  if (!$("token").value) {
    $("access-dialog").showModal();
    throw new Error(
      "Connect to your workspace before uploading or checking client documents.",
    );
  }
  const response = await fetch(path, {
    method: body ? "POST" : "GET",
    headers: {
      "X-CQC-Token": $("token").value,
      "Content-Type": "application/json",
    },
    ...(body ? { body: JSON.stringify(body) } : {}),
  });
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || "Request failed");
  return data;
}
async function perform(fn, button) {
  if (button) button.disabled = true;
  notice("");
  try {
    await fn();
  } catch (error) {
    if ($("intake-dialog").open) $("intake-error").textContent = error.message;
    else notice(error.message);
  } finally {
    if (button && !["approve-letter", "approve-top"].includes(button.id))
      button.disabled = false;
  }
}
function event(id, fn) {
  $(id).addEventListener("click", () => perform(fn, $(id)));
}
function setBanner(title, description, ready = false) {
  $("banner-title").textContent = title;
  $("banner-description").textContent = description;
  $("approval-banner").classList.toggle("ready", ready);
  $("banner-symbol").textContent = ready ? "✓" : "!";
}
function status(row) {
  if (row.status.startsWith("MATCH"))
    return { key: "protected", label: "MATCH", class: "row-protected" };
  if (row.status.startsWith("RELATED"))
    return { key: "related", label: "RELATED", class: "row-hold" };
  if (row.status.startsWith("CHECK"))
    return { key: "check", label: "CHECK", class: "row-hold" };
  if (row.status.startsWith("DISPUTE"))
    return { key: "eligible", label: "QA VERIFIED", class: "row-eligible" };
  return { key: "unmatched", label: "NO MATCH", class: "" };
}
function accountLabel(row) {
  if (row.confirmed_account)
    return `${row.confirmed_account.name} (${row.confirmed_account.status})`;
  return (
    row.candidates
      .map((c) => `${c.account.name} (${c.account.status})`)
      .join("; ") || "None identified"
  );
}
function showSummary(data) {
  $("summary").replaceChildren();
  for (const [key, label] of Object.entries(LABELS)) {
    const card = node("div", undefined, "metric");
    card.append(node("strong", data?.summary[key] ?? "—"), node("span", label));
    $("summary").append(card);
  }
}
function showScreening(data) {
  screening = data;
  invalidate("Complete the letter cross-check before approval.");
  $("report").value = JSON.stringify(data.source_report, null, 2);
  $("all-count").textContent = data.summary.total;
  $("queue-count").textContent =
    data.summary.total - data.summary.qa_candidates;
  $("protection-count").textContent =
    `${data.summary.total} inquiries screened · 3 bureaus`;
  $("protection-progress").style.width = data.summary.total ? "100%" : "0%";
  showSummary(data);
  renderTable();
  renderIssues();
  refreshActivity();
}
function renderTable() {
  const tbody = $("inquiry-rows");
  tbody.replaceChildren();
  const query = $("search").value.toLowerCase(),
    filter = $("status-filter").value;
  const rows = (screening?.rows || []).filter((row) => {
    const s = status(row);
    return (
      (activeBureau === "all" || row.inquiry.bureau === activeBureau) &&
      `${row.inquiry.name} ${accountLabel(row)} ${row.inquiry.date}`
        .toLowerCase()
        .includes(query) &&
      (filter === "all" ||
        (filter === "hold" &&
          ["protected", "related", "check"].includes(s.key)) ||
        (filter === "eligible" && s.key === "eligible") ||
        (filter === "unmatched" && s.key === "unmatched") ||
        (filter === "duplicates" && row.duplicate_ids.length))
    );
  });
  for (const row of rows) {
    const s = status(row),
      tr = node("tr", undefined, s.class);
    tr.append(
      node("td", BUREAUS[row.inquiry.bureau]),
      node("td", row.inquiry.name),
      node("td", row.inquiry.date),
      node("td", accountLabel(row)),
    );
    tr.children[1].title = row.inquiry.name;
    tr.children[3].title = accountLabel(row);
    const td = node("td"),
      badge = node("span", s.label, `status-badge ${s.key}`);
    badge.title = row.status;
    td.append(badge);
    tr.append(td);
    const action = node("td"),
      button = node("button", "View details", "detail-button");
    button.addEventListener("click", () => selectReview(row));
    action.append(button);
    tr.append(action);
    tbody.append(tr);
  }
  if (!rows.length) {
    const tr = node("tr"),
      td = node(
        "td",
        screening
          ? "No inquiries match the selected filters."
          : "Upload and verify a source report to screen its inquiries.",
        "table-empty",
      );
    td.colSpan = 6;
    tr.append(td);
    tbody.append(tr);
  }
  $("table-count").textContent =
    `${rows.length} of ${screening?.summary.total || 0} inquiries shown`;
}
function issueGroup(title, items, kind) {
  if (!items.length) return;
  const group = node("section", undefined, "issue-group"),
    heading = node("div", title, `issue-group-header ${kind}`);
  heading.append(node("span", items.length));
  group.append(heading);
  for (const item of items) {
    const row = node("div", undefined, `issue-row ${kind}`),
      copy = node("div", undefined, "issue-copy");
    copy.append(node("strong", item.title), node("p", item.detail));
    row.append(
      node("span", kind === "review" ? "✓" : "!", "issue-symbol"),
      copy,
      node(
        "span",
        kind === "review"
          ? "Review"
          : kind === "warning"
            ? "Warning"
            : "Critical",
        "severity",
      ),
    );
    if (item.row) {
      row.tabIndex = 0;
      row.setAttribute("role", "button");
      row.setAttribute("aria-label", `Review ${item.row.inquiry.name}`);
      row.addEventListener("click", () => selectReview(item.row));
      row.addEventListener("keydown", (e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          selectReview(item.row);
        }
      });
    }
    group.append(row);
  }
  $("issues-list").append(group);
}
function renderIssues() {
  const critical = [],
    warnings = [],
    reviews = [];
  if (letterCheck) {
    for (const check of letterCheck.checks)
      for (const issue of check.issues) {
        const target = check.matching_results[0];
        const item = {
          title: `${check.entry.name} · ${check.entry.bureau}`,
          detail: issue,
          row: target,
        };
        if (issue.includes("Duplicate")) warnings.push(item);
        else if (issue.includes("Authorization")) reviews.push(item);
        else critical.push(item);
      }
    if (letterCheck.blocking_document_issue)
      critical.push({
        title: "Document extraction incomplete",
        detail: letterCheck.blocking_document_issue,
      });
    if (
      letterCheck.requires_document_verification &&
      letterCheck.state !== "READY TO SEND"
    )
      reviews.push({
        title: "Full letter verification required",
        detail:
          "QA must compare the original letter with every extracted entry before final approval.",
      });
    if (letterCheck.state === "QA LETTER APPROVAL REQUIRED")
      reviews.push({
        title: "Final QA approval required",
        detail:
          "All included inquiries have a documented QA dispute basis. Review the final letter and provide approval evidence.",
      });
  } else if (screening) {
    for (const row of screening.rows) {
      const s = status(row),
        item = {
          title: `${row.inquiry.name} · ${row.inquiry.bureau}`,
          detail: `${row.status}. ${accountLabel(row)}.`,
          row,
        };
      if (s.key === "protected") critical.push(item);
      else if (["related", "check"].includes(s.key)) warnings.push(item);
      else if (s.key === "unmatched") reviews.push(item);
      if (row.duplicate_ids.length)
        warnings.push({
          title: "Duplicate source inquiry",
          detail: `${row.inquiry.name} · ${row.inquiry.date}. Entries are retained; verify duplicates.`,
          row,
        });
    }
  }
  $("issues-list").replaceChildren();
  const count = critical.length + warnings.length + reviews.length;
  $("issue-count").textContent = count
    ? `${count} issue${count === 1 ? "" : "s"} total`
    : letterCheck?.state === "READY TO SEND"
      ? "QA approved"
      : screening
        ? "No flags · check letter"
        : "Not checked";
  issueGroup("Inquiry & Document Errors", critical, "critical");
  issueGroup("Protection Holds & Duplicate Review", warnings, "warning");
  issueGroup("Required Review", reviews, "review");
  if (!count) {
    const empty = node("div", undefined, "empty-state");
    empty.append(
      node(
        "h3",
        letterCheck?.state === "READY TO SEND"
          ? "Final letter approved"
          : "Ready for a careful review",
      ),
      node(
        "p",
        letterCheck?.state === "READY TO SEND"
          ? "The current letter has completed documented QA approval."
          : "Upload both documents, verify extracted entries, and run cross-checking.",
      ),
    );
    $("issues-list").append(empty);
  }
  renderPreview();
}
function renderPreview() {
  const text = letterDoc?.text || $("letter").value;
  if (!text) return;
  $("letter-preview").replaceChildren();
  for (const line of text.split("\n")) {
    const el = node("div", line, "letter-line");
    const checks = (letterCheck?.checks || []).filter(
      (c) => c.issues.length && line.includes(c.entry.name),
    );
    if (checks.length)
      el.classList.add(
        checks.some((c) =>
          c.issues.some(
            (i) => i.includes("CRITICAL") || i.includes("not present"),
          ),
        )
          ? "highlight-critical"
          : "highlight-review",
      );
    $("letter-preview").append(el);
  }
  $("preview-file-name").textContent =
    letterDoc?.filename || "Pasted inquiry schedule";
  $("preview-label").textContent =
    letterCheck?.state === "READY TO SEND"
      ? "QA approved"
      : letterCheck
        ? "Cross-checked"
        : "Extraction review";
  $("edit-extraction").disabled = !letterDoc;
  $("preview-footer").textContent = letterDoc
    ? "Original document retained · highlighted entries need attention"
    : "Inquiry schedule preview · no original letter attached";
}
async function refreshActivity() {
  if (!session || !screening) return;
  try {
    const items = await api(
      `/api/activity/${encodeURIComponent($("report-id").value)}`,
    );
    $("activity").replaceChildren();
    const labels = {
      report_saved: "Source report screened",
      inquiry_reviewed: "QA review recorded",
      letter_approved: "Final letter approved",
      document_uploaded: "Document uploaded",
    };
    for (const item of items) {
      const line = node("div", undefined, "timeline-item"),
        time = new Date(item.at),
        copy = node("div", undefined, "timeline-copy");
      copy.append(
        node("strong", labels[item.action] || item.action),
        node("span", item.actor),
      );
      line.append(
        node(
          "div",
          `${time.toLocaleDateString(undefined, { month: "short", day: "numeric" })}\n${time.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" })}`,
          "timeline-time",
        ),
        node("div", undefined, "timeline-line"),
        copy,
      );
      $("activity").append(line);
    }
    if (!items.length)
      $("activity").append(
        node("p", "No saved activity for this check.", "muted"),
      );
  } catch (e) {
    notice(e.message);
  }
}
function field(record, key, options, label) {
  const input = options ? node("select") : node("input");
  input.dataset.key = key;
  input.setAttribute("aria-label", label || key);
  if (options)
    for (const [value, text] of options) {
      const option = node("option", text);
      option.value = value;
      input.append(option);
    }
  if (key === "date" || key === "opened") input.type = "date";
  input.value = record[key] || "";
  return input;
}
function addRecord(kind, record = {}) {
  const container = $(
      kind === "inquiry" ? "intake-inquiries" : "intake-accounts",
    ),
    card = node("div", undefined, "editable-record");
  card.dataset.record = JSON.stringify({
    ...record,
    id: record.id || `${kind}-${crypto.randomUUID()}`,
  });
  const top = node("div", undefined, "record-top"),
    name = field(record, "name", null, "Original creditor name");
  name.placeholder =
    kind === "inquiry" ? "Inquiry creditor name" : "Positive account creditor";
  const remove = node("button", "×", "remove-record");
  remove.setAttribute("aria-label", `Remove ${kind} entry`);
  remove.addEventListener("click", () => {
    card.remove();
    $("intake-confirm").checked = false;
  });
  top.append(
    field(
      record,
      "bureau",
      [["", "Bureau"], ...Object.entries(BUREAUS)],
      "Bureau",
    ),
    name,
    remove,
  );
  card.append(top);
  const fields = node("div", undefined, "record-fields");
  const specs =
    kind === "inquiry"
      ? [["date", null, "Inquiry date"]]
      : [
          ["opened", null, "Opened date (if known)"],
          [
            "status",
            [
              ["", "Status"],
              ["open", "Open"],
              ["closed", "Closed"],
            ],
            "Account status",
          ],
          [
            "payment_history",
            [
              ["", "History"],
              ["positive", "Positive"],
              ["negative", "Negative"],
              ["unknown", "Unknown"],
            ],
            "Payment history",
          ],
          ["product", null, "Credit product"],
        ];
  for (const [key, options, title] of specs) {
    const label = node("label", title);
    label.append(field(record, key, options, title));
    fields.append(label);
  }
  card.append(fields);
  card.addEventListener("input", () => {
    $("intake-confirm").checked = false;
  });
  container.append(card);
}
function intakeRecords(id) {
  return [...$(id).children].map((card) => {
    const record = JSON.parse(card.dataset.record);
    for (const input of card.querySelectorAll("[data-key]"))
      record[input.dataset.key] = input.value;
    return record;
  });
}
function openIntake(document) {
  intakeDoc = document;
  $("intake-title").textContent =
    document.kind === "source"
      ? "Verify the source report"
      : "Verify the letter’s inquiry list";
  $("intake-filename").textContent = document.filename;
  $("intake-text").textContent = document.text || "No readable text extracted.";
  $("intake-notes").replaceChildren(...document.notes.map((n) => node("p", n)));
  $("intake-inquiries").replaceChildren();
  $("intake-accounts").replaceChildren();
  for (const record of document.draft.inquiries) addRecord("inquiry", record);
  for (const record of document.draft.accounts) addRecord("account", record);
  $("intake-account-section").hidden = document.kind !== "source";
  $("intake-confirm").checked = false;
  $("intake-error").textContent = "";
  $("apply-extraction").disabled = Boolean(document.unreadable_pages?.length);
  if (!$("intake-dialog").open) $("intake-dialog").showModal();
}
function toCSV(entries) {
  const cell = (v) => `"${String(v).replaceAll('"', '""')}"`;
  return (
    "bureau,name,date\n" +
    entries
      .map((i) => [i.bureau, i.name, i.date].map(cell).join(","))
      .join("\n")
  );
}
async function upload(file, kind) {
  if (!file) return;
  if (file.size > 10 * 1024 * 1024)
    throw new Error(
      "File exceeds 10 MB. Split the document and upload a smaller file.",
    );
  invalidate("New document uploaded. Verify the extracted entries.");
  const uploadGeneration = generation;
  if (kind === "source") {
    screening = null;
    sourceDoc = null;
    $("report").value = "";
    showSummary(null);
    renderTable();
  } else {
    letterDoc = null;
    letterVerified = false;
    $("letter").value = "";
    $("edit-extraction").disabled = true;
  }
  $(`${kind}-file-status`).textContent = "Extracting document…";
  renderIssues();
  const bytes = new Uint8Array(await file.arrayBuffer());
  let binary = "";
  for (let i = 0; i < bytes.length; i += 16384)
    binary += String.fromCharCode(...bytes.subarray(i, i + 16384));
  try {
    const document = await api("/api/document", {
      kind,
      filename: file.name,
      content_base64: btoa(binary),
    });
    if (uploadGeneration !== generation)
      throw new Error(
        "A newer document selection replaced this upload. Review the latest uploaded document.",
      );
    if (kind === "source") {
      sourceDoc = document;
      $("review-source").disabled = false;
    } else {
      letterDoc = document;
      renderPreview();
    }
    $(`${kind}-file-name`).textContent = document.filename;
    $(`${kind}-file-status`).textContent =
      "Uploaded · verify extracted entries";
    openIntake(document);
  } catch (error) {
    $(`${kind}-file-status`).textContent = "Upload failed · try again";
    throw error;
  }
}
async function saveSource(report, document = sourceDoc) {
  const checkId = $("report-id").value,
    saveGeneration = generation;
  const body = { id: checkId, report };
  if (document) {
    body.document_id = document.id;
    body.source_verified = true;
  }
  const saved = await api("/api/report", body);
  if (checkId !== $("report-id").value || saveGeneration !== generation)
    throw new Error(
      "Source or check ID changed while saving. Reload the selected check.",
    );
  showScreening(saved);
  $("manual-source-verified").checked = true;
  if (document)
    $("source-file-status").textContent = "Verified · source report screened";
}
event("apply-extraction", async () => {
  if (!$("intake-confirm").checked)
    throw new Error(
      "Verify the full original document and confirm extraction completeness.",
    );
  const inquiries = intakeRecords("intake-inquiries"),
    accounts = intakeRecords("intake-accounts");
  if (!inquiries.length)
    throw new Error("Add at least one inquiry from the document.");
  for (const entry of inquiries) {
    if (!entry.name.trim() || !entry.bureau || !entry.date)
      throw new Error(
        "Every inquiry needs its original creditor name, bureau, and exact date.",
      );
  }
  intakeDoc.draft = { ...intakeDoc.draft, inquiries, accounts };
  if (intakeDoc.kind === "source") await saveSource(intakeDoc.draft, intakeDoc);
  else {
    letterDoc = intakeDoc;
    letterVerified = true;
    $("letter").value = toCSV(inquiries);
    invalidate("Letter entries verified. Run cross-checking.");
    $("letter-file-status").textContent =
      `Verified · ${inquiries.length} inquiry entries`;
    renderPreview();
  }
  $("intake-dialog").close();
  notice("Extracted entries verified. Original documents are preserved.", true);
});
for (const kind of ["source", "letter"]) {
  $(`${kind}-file`).addEventListener("change", (e) =>
    perform(() => upload(e.target.files[0], kind)),
  );
  const zone = $(`${kind}-drop`);
  zone.addEventListener("dragover", (e) => {
    e.preventDefault();
    zone.classList.add("drag-over");
  });
  zone.addEventListener("dragleave", () => zone.classList.remove("drag-over"));
  zone.addEventListener("drop", (e) => {
    e.preventDefault();
    zone.classList.remove("drag-over");
    perform(() => upload(e.dataTransfer.files[0], kind));
  });
}
event("add-inquiry", () => {
  addRecord("inquiry");
  $("intake-confirm").checked = false;
});
event("add-account", () => {
  addRecord("account");
  $("intake-confirm").checked = false;
});
event("edit-extraction", () => openIntake(letterDoc));
event("review-source", () => openIntake(sourceDoc));
async function checkLetter() {
  if (!screening) throw new Error("Upload and verify the source report first.");
  if (letterDoc && !letterVerified)
    throw new Error("Review and verify the extracted letter entries first.");
  if (!$("letter").value.trim())
    throw new Error("Upload a letter or enter its inquiry schedule.");
  const runGeneration = generation,
    data = await api("/api/letter/check", {
      report_id: $("report-id").value,
      text: $("letter").value,
      ...(letterDoc ? { document_id: letterDoc.id } : {}),
    });
  if (runGeneration !== generation)
    throw new Error("Documents changed during checking. Run the check again.");
  letterCheck = data;
  showLetter(data);
  refreshActivity();
}
function showLetter(data) {
  letterCheck = data;
  const canApprove =
    data.state === "QA LETTER APPROVAL REQUIRED" && session?.role === "qa";
  $("approve-letter").disabled = !canApprove;
  $("approve-top").disabled = !canApprove;
  $("letter-results").textContent =
    data.state === "BLOCKED"
      ? "Approval blocked. Resolve highlighted entries and submit an updated final letter."
      : data.state === "READY TO SEND"
        ? `READY TO SEND · approved by ${data.approved_by}`
        : "Inquiry screening complete. Final QA review and approval evidence are required.";
  setBanner(
    data.state === "BLOCKED"
      ? "Approval blocked until all critical issues are resolved."
      : data.state === "READY TO SEND"
        ? "Final letter approved — ready to send."
        : "Final QA approval required. ",
    data.state === "BLOCKED"
      ? "The letter includes protected, unresolved, duplicate, or inaccurate entries. Review each issue before proceeding."
      : data.state === "READY TO SEND"
        ? "The current document and inquiry schedule have completed documented QA review."
        : "Review the full original letter and document your approval before proceeding.",
    data.state === "READY TO SEND",
  );
  renderIssues();
}
async function approveLetter() {
  if (!letterCheck || letterCheck.state !== "QA LETTER APPROVAL REQUIRED")
    throw new Error(
      "Cross-check the current letter and resolve all issues first.",
    );
  if (!$("confirm-letter").checked)
    throw new Error(
      "Confirm that you reviewed the complete original letter and its inquiry list.",
    );
  const runGeneration = generation,
    data = await api("/api/letter/approve", {
      report_id: $("report-id").value,
      text: $("letter").value,
      fingerprint: letterCheck.fingerprint,
      letter_hash: letterCheck.letter_hash,
      approval_evidence: $("approval-evidence").value,
      document_verified: $("confirm-letter").checked,
      ...(letterDoc ? { document_id: letterDoc.id } : {}),
    });
  if (runGeneration !== generation)
    throw new Error(
      "Documents changed while approving. Reload and cross-check again.",
    );
  showLetter(data);
  refreshActivity();
}
event("run-check", checkLetter);
event("check-letter", checkLetter);
event("approve-letter", approveLetter);
event("approve-top", () => {
  scrollToId("letter-controls");
  notice(
    "Review the complete letter, confirm its inquiry list, and provide final approval evidence below.",
  );
});
function selectReview(row) {
  selectedInquiry = row;
  $("qa-form").hidden = false;
  $("qa-form").reset();
  $("qa-selection").textContent =
    `${row.inquiry.name} · ${row.inquiry.bureau} · ${row.inquiry.date} — ${row.status}`;
  $("qa-evidence").replaceChildren(
    node(
      "p",
      `${row.accounts_checked} protected accounts checked across all bureaus. Responsible agent: ${row.responsible_agent}`,
      "muted",
    ),
  );
  for (const candidate of row.candidates) {
    const card = node("div", undefined, "evidence-card");
    card.append(
      node(
        "strong",
        `${candidate.account.name} · ${candidate.account.bureau} · ${candidate.account.status}`,
      ),
    );
    for (const evidence of candidate.evidence)
      card.append(
        node(
          "p",
          `${evidence.verified_fact ? "Verified affiliation fact" : "Investigation suggestion"}: ${evidence.evidence}`,
        ),
      );
    card.append(node("p", candidate.required_verification));
    $("qa-evidence").append(card);
  }
  if (row.review)
    $("qa-evidence").append(node("pre", JSON.stringify(row.review, null, 2)));
  $("qa-account").replaceChildren();
  const empty = node("option", "Choose confirmed account");
  empty.value = "";
  $("qa-account").append(empty);
  $("qa-account-findings").replaceChildren();
  for (const account of screening.source_report.accounts.filter(
    (a) => a.status === "open" || a.payment_history === "positive",
  )) {
    const option = node("option", `${account.name} · ${account.bureau}`);
    option.value = account.id;
    $("qa-account").append(option);
    const label = node(
        "label",
        `${account.name} (${account.bureau}, ${account.status})`,
      ),
      area = node("textarea");
    area.rows = 2;
    area.required = true;
    area.dataset.accountId = account.id;
    area.setAttribute("aria-label", `Findings for ${account.name}`);
    label.append(area);
    $("qa-account-findings").append(label);
  }
  $("qa-checklist").replaceChildren();
  for (const [key, title] of Object.entries(CHECKS)) {
    const label = node("label", undefined, "checkbox-label"),
      input = node("input");
    input.type = "checkbox";
    input.required = true;
    input.dataset.check = key;
    label.append(input, document.createTextNode(title));
    $("qa-checklist").append(label);
  }
  scrollToId("qa-panel");
}
$("qa-form").addEventListener("submit", (e) => {
  e.preventDefault();
  perform(async () => {
    if (!selectedInquiry || !screening)
      throw new Error("Reload screening and select an inquiry first.");
    const eligible = $("qa-outcome").value === "eligible";
    const review = {
      fingerprint: screening.fingerprint,
      outcome: $("qa-outcome").value,
      matched_account_id: $("qa-account").value,
      evidence: $("qa-evidence-text").value,
      authorization_findings: $("qa-authorization").value,
      application_findings: $("qa-application").value,
      date_findings: $("qa-dates").value,
      affiliation_findings: $("qa-affiliations").value,
      auto_financing_findings: $("qa-auto").value,
      factual_basis: $("qa-basis").value,
      authorization_result: eligible ? "documented_basis_to_challenge" : "",
      account_findings: Object.fromEntries(
        [...$("qa-account-findings").querySelectorAll("textarea")].map((el) => [
          el.dataset.accountId,
          el.value,
        ]),
      ),
      checklist: Object.fromEntries(
        [...$("qa-checklist").querySelectorAll("input")].map((el) => [
          el.dataset.check,
          el.checked,
        ]),
      ),
    };
    showScreening(
      await api("/api/review", {
        report_id: $("report-id").value,
        inquiry_id: selectedInquiry.inquiry.id,
        review,
      }),
    );
    $("qa-form").hidden = true;
    selectedInquiry = null;
    notice(
      "QA decision recorded. Cross-check the current final letter again.",
      true,
    );
  }, e.submitter);
});
$("qa-outcome").addEventListener("change", () => {
  $("qa-basis").required = $("qa-outcome").value === "eligible";
  $("qa-account").disabled = $("qa-outcome").value === "eligible";
});
event("screen", async () => {
  if (!$("manual-source-verified").checked)
    throw new Error(
      "Confirm that the source inquiry and account lists are complete.",
    );
  await saveSource(JSON.parse($("report").value));
});
event("reload", async () => {
  showScreening(
    await api(`/api/report/${encodeURIComponent($("report-id").value)}`),
  );
  const id = screening.source_report._document_id;
  if (id) {
    sourceDoc = await api(`/api/document/${id}`);
    sourceDoc.draft = screening.source_report;
    $("source-file-name").textContent = sourceDoc.filename;
    $("source-file-status").textContent = "Saved source · verified";
    $("review-source").disabled = false;
  } else {
    sourceDoc = null;
    $("review-source").disabled = true;
  }
});
function previewOriginal() {
  const text = letterDoc?.text || $("letter").value;
  if (!text) throw new Error("Upload a letter first.");
  $("expanded-text").textContent = text;
  $("preview-dialog").showModal();
}
event("expand-preview", previewOriginal);
event("view-original", async () => {
  if (!letterDoc?.id) {
    previewOriginal();
    return;
  }
  const document = await api(`/api/document/${letterDoc.id}`),
    bytes = Uint8Array.from(atob(document.content_base64), (ch) =>
      ch.charCodeAt(0),
    );
  const link = node("a");
  const url = URL.createObjectURL(
    new Blob([bytes], { type: "application/octet-stream" }),
  );
  link.href = url;
  link.download = document.filename;
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
});
event("upload-letter-shortcut", () => $("letter-file").click());
event("resolve-issues", () => {
  const unresolved = screening?.rows.find(
    (row) => status(row).key !== "eligible",
  );
  if (unresolved) selectReview(unresolved);
  else scrollToId("issues-panel");
});
for (const button of document.querySelectorAll("[data-target]"))
  button.addEventListener("click", () => {
    document
      .querySelectorAll(".nav-item")
      .forEach((n) => n.classList.remove("active"));
    button.classList.add("active");
    scrollToId(button.dataset.target);
  });
for (const button of document.querySelectorAll("[data-close]"))
  button.addEventListener("click", () => $(button.dataset.close).close());
event("settings-nav", () => $("access-dialog").showModal());
event("access-button", () => $("access-dialog").showModal());
event("connect", async () => {
  session = await api("/api/session");
  $("session").textContent = `${session.actor} · ${session.role}`;
  $("user-name").textContent = session.actor;
  $("user-role").textContent =
    `${session.role.toUpperCase()} · Credit quality team`;
  $("avatar").textContent = session.actor.slice(0, 2).toUpperCase();
  $("access-dialog").close();
  if (letterCheck) showLetter(letterCheck);
  notice("Connected to workspace.", true);
});
$("token").addEventListener("input", () => {
  session = null;
  invalidate("Workspace identity changed. Connect and cross-check again.");
});
event("refresh-activity", refreshActivity);
$("search").addEventListener("input", renderTable);
$("status-filter").addEventListener("change", renderTable);
for (const button of document.querySelectorAll("[data-bureau]"))
  button.addEventListener("click", () => {
    activeBureau = button.dataset.bureau;
    document
      .querySelectorAll(".tab")
      .forEach((n) => n.classList.remove("active"));
    button.classList.add("active");
    renderTable();
  });
$("letter").addEventListener("input", () => {
  invalidate("Inquiry schedule changed. Cross-check again.");
  renderPreview();
});
$("report").addEventListener("input", () => {
  screening = null;
  $("manual-source-verified").checked = false;
  invalidate("Source data changed. Verify and save the report again.");
  showSummary(null);
  renderTable();
});
$("report-id").addEventListener("input", () => {
  screening = null;
  sourceDoc = null;
  $("review-source").disabled = true;
  selectedInquiry = null;
  $("qa-form").hidden = true;
  invalidate("Check ID changed. Load or import its source report.");
  showSummary(null);
  renderTable();
  renderIssues();
});
$("relationship").value = JSON.stringify(
  {
    id: "",
    inquiry_company: "",
    official_creditor: "",
    known_variations: [],
    parent_or_subsidiary: "",
    issuing_bank: "",
    financing_partner: "",
    related_products: [],
    relationship_type: "alias",
    evidence_source: "",
    valid_from: null,
    valid_to: null,
  },
  null,
  2,
);
event("registry", async () => {
  $("registry-results").textContent = JSON.stringify(
    await api("/api/relationships"),
    null,
    2,
  );
});
event("save-relationship", async () => {
  await api("/api/relationship", {
    record: JSON.parse($("relationship").value),
    verify: $("verify").checked,
  });
  screening = null;
  invalidate("Relationship evidence changed. Reload screening.");
  showSummary(null);
  renderTable();
  notice(
    "Relationship saved. Reload screening to invalidate and refresh prior QA decisions.",
    true,
  );
});
event("example", async () => {
  const report = {
    inquiries: [
      { id: "i1", bureau: "EX", name: "EXAMPLE BANK", date: "2026-05-10" },
      {
        id: "i2",
        bureau: "TU",
        name: "EXAMPLE AUTO DEALER",
        date: "2026-04-15",
      },
      { id: "i3", bureau: "EQ", name: "EXAMPLE LENDER", date: "2026-06-12" },
    ],
    accounts: [
      {
        id: "a1",
        bureau: "TU",
        name: "EXAMPLE BANK",
        status: "open",
        payment_history: "positive",
        product: "credit_card",
        opened: "2026-05-12",
      },
      {
        id: "a2",
        bureau: "EX",
        name: "EXAMPLE AUTO FINANCE",
        status: "open",
        payment_history: "positive",
        product: "auto_loan",
        opened: "2026-04-20",
      },
    ],
  };
  sourceDoc = null;
  $("report-id").value = "illustrative-example";
  letterDoc = {
    filename: "Illustrative dispute letter.txt",
    kind: "letter",
    text: "ILLUSTRATIVE DATA — NOT A CLIENT LETTER\n\nTo Whom It May Concern,\nPlease review the following inquiries.\n\nExperian\n1. EXAMPLE BANK (Inquiry dated 05/10/2026)\n\nTransUnion\n2. EXAMPLE AUTO DEALER (Inquiry dated 04/15/2026)\n\nEquifax\n3. EXAMPLE LENDER (Inquiry dated 06/12/2026)\n\nSincerely,\nExample Consumer",
    draft: report,
    notes: ["Illustrative data only. No creditor affiliations are asserted."],
    unreadable_pages: [],
  };
  letterVerified = true;
  $("letter").value = toCSV(report.inquiries);
  $("source-file-name").textContent = "Illustrative source report";
  $("source-file-status").textContent = "Example only · not client data";
  $("letter-file-name").textContent = letterDoc.filename;
  $("letter-file-status").textContent = "Example only · not client data";
  $("report").value = JSON.stringify(report, null, 2);
  renderPreview();
  await saveSource(report, null);
  await checkLetter();
  notice(
    "Illustrative example loaded. All matches still require documented verification.",
    true,
  );
});
showSummary(null);
