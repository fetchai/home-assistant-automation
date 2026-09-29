import { ref, computed } from "vue";

const agentBaseUrl = ".";

// Every endpoint is POST, including the read-only ones: proxy.py answers GET
// out of the built panel directory and only forwards POST to the agent.
async function _post(path, body = {}) {
  const res = await fetch(`${agentBaseUrl}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return await res.json();
}

// What the agent is allowed to watch, and what it has recorded. Two tabs share
// this one composable because they share one idea — the selection governs the
// record — and keeping them apart would mean the Record tab could not tell a
// watched entity with no data from an entity nobody selected.
export function useObservation() {
  // --- the picker -----------------------------------------------------
  const areas = ref([]);
  const hiddenCount = ref(0);
  // The saved selection, and the edited one. Kept apart so Save has something
  // to send and Discard has something to go back to.
  const savedDevices = ref([]);
  const savedEntities = ref([]);
  const draftDevices = ref(new Set());
  const draftEntities = ref(new Set());
  const filter = ref("");

  // --- the record -----------------------------------------------------
  const status = ref({});
  const rows = ref([]);
  const totals = ref({});
  const relations = ref([]);
  const detail = ref(null);

  const loading = ref(false);
  const saving = ref(false);
  const error = ref("");

  const dirty = computed(() => {
    const sameDevices =
      draftDevices.value.size === savedDevices.value.length &&
      savedDevices.value.every((d) => draftDevices.value.has(d));
    const sameEntities =
      draftEntities.value.size === savedEntities.value.length &&
      savedEntities.value.every((e) => draftEntities.value.has(e));
    return !(sameDevices && sameEntities);
  });

  // An entity is watched either because it was picked, or because its whole
  // device was — the same union the agent resolves server-side, mirrored here
  // so the checkboxes react without a round trip.
  function isEntityWatched(entity, deviceId) {
    return draftEntities.value.has(entity.entity_id) ||
      (deviceId ? draftDevices.value.has(deviceId) : false);
  }

  function deviceState(device) {
    if (draftDevices.value.has(device.device_id)) return "all";
    const picked = device.entities.filter((e) => draftEntities.value.has(e.entity_id)).length;
    if (picked === 0) return "none";
    return picked === device.entities.length ? "all" : "some";
  }

  const watchedCount = computed(() => {
    let total = 0;
    for (const area of areas.value) {
      for (const device of area.devices) {
        total += device.entities.filter((e) => isEntityWatched(e, device.device_id)).length;
      }
      total += area.entities.filter((e) => isEntityWatched(e, null)).length;
    }
    return total;
  });

  // Filtering hides rows; it must never change what is selected, so it is
  // applied at render time and the draft sets are left alone.
  const visibleAreas = computed(() => {
    const needle = filter.value.trim().toLowerCase();
    if (!needle) return areas.value;
    const matches = (e) =>
      e.name.toLowerCase().includes(needle) || e.entity_id.toLowerCase().includes(needle);

    const out = [];
    for (const area of areas.value) {
      const devices = area.devices
        .map((d) => {
          // A device matching by name keeps all of its entities, so searching
          // for "fridge" offers the whole thing rather than nothing.
          if (d.name.toLowerCase().includes(needle)) return d;
          const entities = d.entities.filter(matches);
          return entities.length ? { ...d, entities } : null;
        })
        .filter(Boolean);
      const entities = area.entities.filter(matches);
      if (devices.length || entities.length || area.name.toLowerCase().includes(needle)) {
        out.push({ ...area, devices, entities });
      }
    }
    return out;
  });

  function _applySelection(data) {
    savedDevices.value = data.selection?.devices || [];
    savedEntities.value = data.selection?.entities || [];
    draftDevices.value = new Set(savedDevices.value);
    draftEntities.value = new Set(savedEntities.value);
  }

  function _applyTree(data) {
    areas.value = data.areas || [];
    hiddenCount.value = data.hidden_count || 0;
    _applySelection(data);
  }

  function toggleDevice(device) {
    const devices = new Set(draftDevices.value);
    const entities = new Set(draftEntities.value);
    const state = deviceState(device);
    if (state === "all") {
      devices.delete(device.device_id);
      // Also clear anything picked individually inside it, or unticking the
      // device would appear to do nothing.
      device.entities.forEach((e) => entities.delete(e.entity_id));
    } else {
      devices.add(device.device_id);
      device.entities.forEach((e) => entities.delete(e.entity_id));
    }
    draftDevices.value = devices;
    draftEntities.value = entities;
  }

  function toggleEntity(entity, device) {
    const devices = new Set(draftDevices.value);
    const entities = new Set(draftEntities.value);
    const watched = isEntityWatched(entity, device?.device_id);

    if (device && devices.has(device.device_id)) {
      // Unticking one entity of a wholly-selected device demotes the device to
      // its individual entities, so the one being removed can actually go.
      devices.delete(device.device_id);
      device.entities.forEach((e) => {
        if (e.entity_id !== entity.entity_id) entities.add(e.entity_id);
      });
    } else if (watched) {
      entities.delete(entity.entity_id);
    } else {
      entities.add(entity.entity_id);
    }
    draftDevices.value = devices;
    draftEntities.value = entities;
  }

  function discard() {
    draftDevices.value = new Set(savedDevices.value);
    draftEntities.value = new Set(savedEntities.value);
  }

  async function _call(fn) {
    error.value = "";
    try {
      return await fn();
    } catch (e) {
      error.value = "Could not reach the agent.";
      return null;
    }
  }

  async function loadRegistry() {
    loading.value = true;
    await _call(async () => {
      const data = await _post("/rest/observation/registry");
      if (data.error) error.value = data.error;
      else _applyTree(data);
    });
    loading.value = false;
  }

  async function saveSelection() {
    saving.value = true;
    await _call(async () => {
      const data = await _post("/rest/observation/selection", {
        devices: [...draftDevices.value],
        entities: [...draftEntities.value],
      });
      // A save the agent refused must not look like a save that worked: the
      // draft is left exactly as it was so the user can retry it.
      if (data.error) error.value = data.error;
      else _applyTree(data);
    });
    saving.value = false;
  }

  async function loadRecord() {
    await _call(async () => {
      const data = await _post("/rest/observation/record");
      if (data.error) {
        error.value = data.error;
        return;
      }
      status.value = data.status || {};
      rows.value = data.entities || [];
      totals.value = data.totals || {};
      relations.value = data.relations || [];
    });
  }

  async function openEntity(entityId) {
    detail.value = { entity_id: entityId, loading: true, recent: [], profile: {} };
    await _call(async () => {
      const data = await _post("/rest/observation/entity", { entity_id: entityId });
      detail.value = { ...data, loading: false };
    });
  }

  function closeEntity() {
    detail.value = null;
  }

  return {
    areas, visibleAreas, filter, hiddenCount, watchedCount, dirty,
    status, rows, totals, relations, detail,
    loading, saving, error,
    isEntityWatched, deviceState, toggleDevice, toggleEntity, discard,
    loadRegistry, saveSelection, loadRecord, openEntity, closeEntity,
  };
}
