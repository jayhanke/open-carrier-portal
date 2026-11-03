"use strict";

(() => {
  const root = document.getElementById("mapping-workspace");
  if (!root) return;
  const get = id => document.getElementById(id);
  const make = (tag, text, className) => {
    const node = document.createElement(tag);
    if (text !== undefined) node.textContent = text;
    if (className) node.className = className;
    return node;
  };
  const option = (select, value, label) => {
    const item = make("option", label);
    item.value = value;
    select.append(item);
  };
  const message = (text, error = false) => {
    get("map-message").textContent = text;
    get("map-message").classList.toggle("error", error);
  };
  const button = (label, action) => {
    const node = make("button", label);
    node.type = "button";
    node.addEventListener("click", action);
    return node;
  };
  let schema, resources, fields, tables, resource, selected = null, pending = null;
  let dirty = false;
  const profile = root.dataset.profile;
  const entry = () => resources[resource];
  const aliases = () => entry() ? [
    {alias: "base", table: entry().table},
    ...(entry().joins || []).map(join => ({alias: join.alias, table: join.table}))
  ] : [];
  const changed = () => {
    dirty = true;
    get("map-sql").textContent = "Draft changed. Run a preview to see the generated SQL.";
    get("map-preview-result").replaceChildren();
    renderContract();
  };
  const renderContract = () => {
    const missing = [];
    for (const [name, required] of Object.entries(schema.required_fields)) {
      if (!schema.core_resources.includes(name) && !resources[name]) continue;
      const count = required.filter(field => !resources[name]?.columns[field]).length;
      if (count) missing.push(schema.resource_labels[name] + " (" + count + ")");
    }
    get("map-contract").textContent = missing.length ?
      "Required fields in core or enabled sections still need mapping: " + missing.join(", ") + "." :
      "Core circuit, port, device, rack and location relationships are mapped. " +
      "Preview and save to validate source columns. Company and rack-position enrichment is optional.";
  };
  const ensureEntry = () => {
    if (!entry()) resources[resource] = {table: get("map-base").value, columns: {}};
    return entry();
  };
  const assign = (field, value, target = resource) => {
    const known = (tables[value.table] || []).includes(value.column);
    if (!known) {
      message("That column is not in the loaded schema.", true);
      return;
    }
    if (!focusResource(target)) return;
    if (!entry()) {
      resources[resource] = {table: value.table, columns: {}};
      get("map-base").value = value.table;
    }
    let matches = aliases().filter(item => item.table === value.table);
    if (value.alias && value.resource === resource) {
      matches = matches.filter(item => item.alias === value.alias);
    }
    if (!matches.length && value.table === get("map-base").value) {
      ensureEntry();
      matches = [{alias: "base", table: value.table}];
    }
    if (matches.length !== 1) {
      pending = {field, value};
      get("map-relationships").open = true;
      get("map-relationships").parentElement.open = true;
      message("Confirm a relationship to " + value.table +
        " in Source configuration before mapping this column. " +
        "For repeated tables, select its alias.", true);
      return;
    }
    if (field === "source_id" && matches[0].alias !== "base") {
      message("source_id must come from the primary source table.", true);
      return;
    }
    ensureEntry().columns[field] = matches[0].alias + "." + value.column;
    pending = null;
    changed();
    renderSlots();
    renderTables();
    renderJoins();
    message("Linked " + value.table + "." + value.column + " \u2192 " +
      schema.resource_labels[resource] + " / " + field + ". Preview before saving.");
  };
  const renderTables = () => {
    const container = get("map-tables");
    const opened = new Map([...container.querySelectorAll("details")]
      .map(node => [node.dataset.table, node.open]));
    container.replaceChildren();
    const search = get("map-search").value.toLowerCase();
    for (const [table, columns] of Object.entries(tables)) {
      const visible = columns.filter(column =>
        (table + "." + column).toLowerCase().includes(search));
      if (!visible.length) continue;
      const instances = aliases().filter(item => item.table === table);
      if (!instances.length) instances.push({alias: null, table});
      for (const instance of instances) {
        const group = make("details");
        group.dataset.table = table + ":" + (instance.alias || "");
        group.open = Boolean(search || selected?.table === table) ||
          (opened.get(group.dataset.table) ?? Boolean(instance.alias));
        const heading = make("summary",
          table + (instance.alias ? " (" + instance.alias + ")" : ""));
        if (Object.values(resources).some(mapping => mapping.table === table ||
            (mapping.joins || []).some(join => join.table === table))) {
          heading.append(make("span", "Linked", "mapper-linked"));
        }
        group.append(heading);
        const renderColumns = () => {
          if (!group.open || group.dataset.loaded) return;
          group.dataset.loaded = "true";
          for (const column of visible) {
            const value = {table, column, alias: instance.alias, resource};
            const node = button(column, () => {
              selected = value;
              renderTables();
              message("Selected " + table + "." + column + ". Choose Assign beside an OCP field.");
            });
            node.className = "mapper-field";
            node.draggable = true;
            node.setAttribute("aria-pressed", String(
              selected?.table === table && selected?.column === column &&
              selected?.alias === instance.alias));
            node.addEventListener("dragstart", event => {
              event.dataTransfer.setData("application/json", JSON.stringify(value));
              event.dataTransfer.effectAllowed = "copy";
            });
            group.append(node);
          }
        };
        group.addEventListener("toggle", renderColumns);
        renderColumns();
        container.append(group);
      }
    }
  };
  const sourceLabel = (mapping, field) => {
    const value = mapping?.columns[field];
    if (!value) return "Drop customer field here";
    if (!value.includes(".")) return mapping.table + "." + value;
    const [alias, column] = value.split(".");
    const table = alias === "base" ? mapping.table :
      mapping.joins?.find(join => join.alias === alias)?.table;
    if (!table) return value;
    const repeated = [{table: mapping.table}, ...(mapping.joins || [])]
      .filter(item => item.table === table).length > 1;
    return table + "." + column + (repeated ? " (" + alias + ")" : "");
  };
  const renderSlots = () => {
    const opened = new Map([...get("map-slots").querySelectorAll("details[data-panel]")]
      .map(node => [node.dataset.panel, node.open]));
    get("map-slots").replaceChildren();
    const panel = (key, label, className, initiallyOpen) => {
      const node = make("details", undefined, className);
      node.dataset.panel = key;
      node.open = opened.get(key) ?? initiallyOpen;
      node.append(make("summary", label));
      return node;
    };
    const labels = {
      source_id: "Identifier", name: "Name", address1: "Address line 1",
      address2: "Address line 2", postal_code: "Postal code",
      site_code: "Site code (legacy)",
      device_id: "Device source ID", rack_id: "Rack source ID",
      location_id: "Location source ID", company_id: "Company source ID",
      owner_company_id: "Owning company source ID", service_id: "Service source ID",
      circuit_layout_id: "Circuit path source ID",
      port_a_id: "A-end port", port_b_id: "Z-end port", port_z_id: "Z-end port",
      position: "Step order", item_type: "Step type", item_source_id: "Component identifier"
    };
    const renderField = (target, name) => {
      const mapping = resources[target];
      const slot = make("div", undefined, "mapper-slot");
      slot.dataset.field = name;
      slot.dataset.resource = target;
      const drop = make("div", undefined,
        "mapper-drop" + (mapping?.columns[name] ? " linked" : ""));
      drop.setAttribute("aria-label", "Database field for " +
        schema.resource_labels[target] + " / " + name);
      drop.append(make("code", sourceLabel(mapping, name)));
      const actions = make("div", undefined, "mapper-slot-actions");
      actions.append(button("Assign", () => {
        if (!selected) return message("Select a database column first.", true);
        assign(name, selected, target);
      }));
      if (mapping?.columns[name]) actions.append(button("Clear", () => {
        if (!focusResource(target)) return;
        delete resources[target].columns[name];
        changed();
        renderSlots();
        message("Unlinked " + schema.resource_labels[target] + " / " + name +
          " in the draft. Save to apply this change.");
      }));
      drop.append(actions);
      const arrow = make("span", "\u2192", "mapper-arrow");
      arrow.setAttribute("aria-hidden", "true");
      const label = make("div", undefined, "mapper-target");
      const title = name === "observium_id" ?
        (target === "ports" ? "Observium port ID" : "Observium device ID") :
        target === "racks" && name === "name" ? "Rack name" :
        labels[name] || name.replaceAll("_", " ");
      label.append(make("code", target + "." + name));
      const monitoringField = (schema.monitoring_fields[target] || []).includes(name);
      const state = schema.required_fields[target].includes(name) ?
        (monitoringField ? "Required mapping for monitoring; NULL allows unmonitored" : "Required") :
        monitoringField ? "Optional when monitoring is disabled" :
        schema.private_fields[target].includes(name) ? "Private \u2014 not shared" : "Optional";
      label.append(make("small", title[0].toUpperCase() + title.slice(1) + " \u00b7 " + state));
      slot.append(drop, arrow, label);
      drop.addEventListener("dragover", event => {
        event.preventDefault();
        event.dataTransfer.dropEffect = "copy";
        drop.classList.add("over");
      });
      drop.addEventListener("dragleave", () => drop.classList.remove("over"));
      drop.addEventListener("drop", event => {
        event.preventDefault();
        drop.classList.remove("over");
        let value;
        try {
          value = JSON.parse(event.dataTransfer.getData("application/json"));
          if (typeof value?.table !== "string" || typeof value?.column !== "string") {
            throw new Error("Invalid column");
          }
        } catch {
          message("Drop a column from the database fields panel.", true);
          return;
        }
        assign(name, value, target);
      });
      return slot;
    };
    const query = get("map-target-search").value.trim().toLowerCase();
    let visibleTables = 0;
    for (const [key, group] of Object.entries(schema.target_tables)) {
      const searchable = [group.label, ...group.resources.flatMap(target =>
        [schema.resource_labels[target], ...fields[target]]), ...(group.api_fields || [])];
      if (query && !searchable.join(" ").toLowerCase().includes(query)) continue;
      visibleTables++;
      const card = panel("table-" + key, group.label, "mapper-ocp-table",
        group.resources.includes(resource));
      card.dataset.targetTable = key;
      if (query) card.open = true;
      else card.setAttribute("name", "ocp-mapping-tables");
      card.querySelector("summary").title = group.description;
      card.querySelector("summary").addEventListener("click", event => {
        if (!focusResource(group.resources[0] || null)) event.preventDefault();
      });
      if (group.resources.length) {
        const headings = make("div", undefined, "mapper-row-headings");
        headings.append(make("span", "Customer database field"), make("span"),
          make("span", "OCP field"));
        card.append(headings);
      }
      for (const target of group.resources) {
        const section = group.resources.length === 1 ?
          make("div", undefined, "mapper-section") :
          panel(target, schema.resource_labels[target], "mapper-section", true);
        section.dataset.resource = target;
        section.classList.toggle("active", resource === target);
        section.querySelector("summary")?.addEventListener("click", event => {
          if (!focusResource(target)) event.preventDefault();
        });
        const advanced = panel("extra-" + target,
          "Additional fields", "mapper-extra", false);
        const names = [...new Set([...(fields[target] || []),
          ...Object.keys(resources[target]?.columns || {})])]
          .filter(name => !(schema.hidden_fields[target] || []).includes(name));
        for (const name of names) {
          const slot = renderField(target, name);
          if (schema.basic_fields[target].includes(name) ||
              schema.required_fields[target].includes(name)) section.append(slot);
          else advanced.append(slot);
        }
        if (query && !group.label.toLowerCase().includes(query) &&
            names.some(name => name.toLowerCase().includes(query))) advanced.open = true;
        if (advanced.children.length > 1) section.append(advanced);
        card.append(section);
      }
      if (group.api_fields?.length) {
        const api = make("div", undefined, "mapper-api-fields");
        api.append(make("h4", "API-supplied fields"));
        api.append(make("p", group.api_note, "mapper-help"));
        for (const name of group.api_fields) {
          const row = make("div", undefined, "mapper-api-field");
          row.dataset.apiField = name;
          row.append(make("code", name), make("span", "API supplied", "mapper-api-badge"));
          api.append(row);
        }
        card.append(api);
      }
      get("map-slots").append(card);
    }
    const total = Object.keys(schema.target_tables).length;
    get("map-target-count").textContent = query ?
      "(" + visibleTables + " of " + total + ")" : "(" + total + ")";
    if (!visibleTables) get("map-slots").append(make("p", "No OCP tables or fields match."));
  };
  const confirmJoin = join => {
    const mapping = ensureEntry();
    if ((mapping.joins || []).length >= 8) {
      return message("A resource can contain at most eight joins.", true);
    }
    if (!tables[join.table]?.includes(join.right)) {
      return message("Choose a target column from the loaded schema.", true);
    }
    mapping.joins ||= [];
    let number = 1;
    while (aliases().some(item => item.alias === "j" + number)) number++;
    join.alias = "j" + number;
    mapping.joins.push(join);
    changed();
    const waiting = pending;
    renderEditor();
    if (waiting && waiting.value.table === join.table) {
      assign(waiting.field, {...waiting.value, alias: join.alias, resource});
    } else {
      message("Join confirmed in the draft. Run a preview to validate its keys and columns.");
    }
  };
  const renderJoins = () => {
    const container = get("map-joins");
    container.replaceChildren();
    for (const [index, join] of (entry()?.joins || []).entries()) {
      const row = make("div", undefined, "mapper-join");
      row.append(make("p", join.kind.toUpperCase() + " JOIN " + join.table + " (" +
        join.alias + ") on " + join.left + " = " + join.alias + "." + join.right));
      row.append(button("Remove this join and dependent mappings", () => {
        const removed = new Set(entry().joins.slice(index).map(item => item.alias));
        entry().joins = entry().joins.slice(0, index);
        for (const [field, column] of Object.entries(entry().columns)) {
          if (removed.has(column.split(".")[0])) delete entry().columns[field];
        }
        changed();
        renderEditor();
      }));
      container.append(row);
    }
    for (const origin of aliases()) {
      const suggestions = schema.relationships.filter(relation =>
        relation.source_table === origin.table && tables[relation.target_table] &&
        !(entry().joins || []).some(join =>
          join.left === origin.alias + "." + relation.source_column &&
          join.table === relation.target_table && join.right === relation.target_column));
      for (const relation of suggestions) {
        const row = make("div", undefined, "mapper-join");
        row.append(make("p", "Foreign-key suggestion: " + origin.alias + "." +
          relation.source_column + " -> " + relation.target_table + "." +
          relation.target_column + ". LEFT JOIN keeps rows with no match."));
        row.append(button("Confirm suggested join", () => confirmJoin({
          table: relation.target_table, left: origin.alias + "." + relation.source_column,
          right: relation.target_column, kind: "left"
        })));
        container.append(row);
      }
    }
    const manual = get("map-add-join");
    manual.replaceChildren();
    const group = make("details");
    group.append(make("summary", "Add a relationship manually"));
    const controls = {};
    for (const [key, label] of [
      ["left", "From column"], ["table", "Join table"], ["right", "Matching column"],
      ["kind", "Join type"]
    ]) {
      const wrapper = make("label", label);
      const select = make("select");
      controls[key] = select;
      wrapper.append(select);
      group.append(wrapper);
    }
    for (const instance of aliases()) {
      for (const column of tables[instance.table] || []) {
        option(controls.left, instance.alias + "." + column, instance.alias + "." + column);
      }
    }
    for (const table of Object.keys(tables)) option(controls.table, table, table);
    const updateRight = () => {
      controls.right.replaceChildren();
      for (const column of tables[controls.table.value] || []) {
        option(controls.right, column, column);
      }
    };
    controls.table.addEventListener("change", updateRight);
    updateRight();
    option(controls.kind, "left", "LEFT JOIN - keep unmatched source rows");
    option(controls.kind, "inner", "INNER JOIN - exclude unmatched source rows");
    group.append(button("Confirm manual join", () => {
      if (!controls.left.value) return message("Select a primary source table first.", true);
      confirmJoin({table: controls.table.value, left: controls.left.value,
        right: controls.right.value, kind: controls.kind.value});
    }));
    manual.append(group);
  };
  const renderEditor = () => {
    get("map-base").value = entry()?.table || "";
    renderTables();
    renderSlots();
    renderJoins();
    renderContract();
    renderSelection();
  };
  const renderSelection = () => {
    get("map-preview").disabled = !resource;
    get("map-base").disabled = !resource;
    get("map-disable").disabled = !resource;
    get("map-disable").textContent = schema.core_resources.includes(resource) ?
      "Remove this core mapping" : "Omit this optional section";
    get("map-active-label").textContent = resource ?
      "Selected for preview and source configuration: " +
      schema.resource_labels[resource] + ". Select another OCP table to change it." :
      "API-supplied fields are read-only. Select a database-mapped OCP table to preview SQL.";
  };
  const focusResource = target => {
    if (root.hasAttribute("aria-busy")) {
      message("Wait for the current preview or save before changing the selected section.");
      return false;
    }
    if (target !== resource) {
      resource = target;
      pending = null;
      get("map-sql").textContent = "Run a preview for this section to see generated SQL.";
      get("map-preview-result").replaceChildren();
    }
    get("map-base").value = entry()?.table || "";
    renderSelection();
    for (const node of get("map-slots").querySelectorAll(".mapper-section")) {
      node.classList.toggle("active", node.dataset.resource === resource);
    }
    renderTables();
    renderJoins();
    return true;
  };
  const request = async operation => {
    if (operation === "preview" && !entry()?.columns.source_id) {
      return message("Map source_id on the selected resource before continuing.", true);
    }
    const controls = [...root.querySelectorAll("input,select,button")];
    controls.forEach(node => node.disabled = true);
    root.setAttribute("aria-busy", "true");
    const body = new FormData();
    body.set("csrf", root.dataset.csrf);
    body.set("name", profile);
    body.set("operation", operation);
    body.set("resource", resource);
    body.set("mapping", JSON.stringify({resources}));
    try {
      const response = await fetch("/admin/mapping/workspace", {method: "POST", body});
      const result = await response.json();
      if (!response.ok) throw new Error(result.error || "Mapping request failed.");
      message(result.message);
      if (result.missing_fields && Object.keys(result.missing_fields).length) {
        message(result.message + " Core or enabled sections still need required fields in " +
          Object.keys(result.missing_fields).join(", ") + ".", true);
      }
      if (operation === "save") dirty = false;
      if (result.sql) get("map-sql").textContent = result.sql + "\n-- LIMIT parameter: 5";
      const preview = get("map-preview-result");
      preview.replaceChildren();
      if (result.rows) {
        if (!result.rows.length) preview.append(make("p", "No matching records."));
        else {
          const table = make("table"), header = make("tr");
          const columns = Object.keys(result.rows[0]);
          columns.forEach(column => header.append(make("th", column)));
          const head = make("thead");
          head.append(header);
          table.append(head);
          const tbody = make("tbody");
          for (const row of result.rows) {
            const tr = make("tr");
            columns.forEach(column => tr.append(make("td", row[column] ?? "NULL")));
            tbody.append(tr);
          }
          table.append(tbody);
          preview.append(table);
        }
      }
    } catch (error) {
      message(error.message || "Request failed. Check connectivity and sign-in, then retry.", true);
    } finally {
      controls.forEach(node => node.disabled = false);
      root.removeAttribute("aria-busy");
      renderSelection();
    }
  };
  const start = async () => {
    get("map-preview").disabled = true;
    get("map-save").disabled = true;
    try {
      const response = await fetch("/admin/mapping/data?profile=" + encodeURIComponent(profile));
      schema = await response.json();
      if (!response.ok) throw new Error(schema.error || "Schema discovery failed.");
      tables = Object.create(null);
      for (const column of schema.columns) (tables[column.table] ||= []).push(column.column);
      if (!Object.keys(tables).length) throw new Error("No tables were found in this database.");
      resources = schema.mapping.resources;
      fields = schema.fields;
      resource = resources.circuits ? "circuits" :
        (Object.keys(resources).find(name => fields[name]) || "circuits");
      option(get("map-base"), "", "Choose a source table or drag a field");
      get("map-base").options[0].disabled = true;
      for (const table of Object.keys(tables)) option(get("map-base"), table, table);
      get("map-base").addEventListener("change", () => {
        if (entry() && !confirm("Changing the source table clears this resource's fields and joins. Continue?")) {
          get("map-base").value = entry().table;
          return;
        }
        resources[resource] = {table: get("map-base").value, columns: {}};
        selected = pending = null;
        changed();
        renderEditor();
        message("Changed the source table for " + schema.resource_labels[resource] +
          ". Map its fields before saving.");
      });
      get("map-disable").addEventListener("click", () => {
        if (!confirm("Remove this resource from the draft? It will not be available through this mapping after saving.")) return;
        delete resources[resource];
        selected = pending = null;
        changed();
        renderEditor();
        message("Removed " + schema.resource_labels[resource] +
          " from the draft. Save to apply this change.");
      });
      get("map-search").addEventListener("input", renderTables);
      get("map-target-search").addEventListener("input", () => {
        renderSlots();
        const cards = get("map-slots").querySelectorAll(".mapper-ocp-table");
        if (cards.length === 1) {
          const table = schema.target_tables[cards[0].dataset.targetTable];
          focusResource(table.resources[0] || null);
        }
      });
      get("map-template").addEventListener("click", () => {
        if (!confirm("Replace this draft with example source mappings? Saved mappings " +
          "will not change until you validate and save.")) return;
        resources = structuredClone(schema.template.resources);
        if (!resource) resource = "circuits";
        selected = pending = null;
        changed();
        renderEditor();
        message("Example mappings loaded as a draft, not detected customer mappings. " +
          "Review every source table and field before saving.");
      });
      get("map-preview").addEventListener("click", () => request("preview"));
      get("map-save").addEventListener("click", () => request("save"));
      renderEditor();
      get("map-preview").disabled = false;
      get("map-save").disabled = false;
      message("Schema loaded. Drag columns or select a column and choose Assign. No data has been copied.");
    } catch (error) {
      message(error.message || "Cannot load database schema. Check Connections and reload.", true);
    }
  };
  get("map-profile").addEventListener("change", event => {
    if (dirty && !confirm("Discard unsaved mapping changes and switch connections?")) {
      event.target.value = profile;
      return;
    }
    dirty = false;
    location.assign("/admin/mapping?profile=" + encodeURIComponent(event.target.value));
  });
  window.addEventListener("beforeunload", event => {
    if (dirty) {
      event.preventDefault();
      event.returnValue = "";
    }
  });
  start();
})();
