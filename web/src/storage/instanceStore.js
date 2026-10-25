import { readLocalPreference, storeLocalPreference } from "./awakeStore.js";
import { settingsResponseError } from "./settingsError.js";

export function identityName(role) {
  return role === "user"
    ? readLocalPreference("lattice.awake.name.user", "User")
    : readLocalPreference("lattice.awake.name.assistant", "AI");
}

export async function instanceSettings(changes) {
  const response = await fetch("/__lattice/settings", {
    method: changes ? "PATCH" : "GET",
    headers: changes ? { "Content-Type": "application/json" } : {},
    ...(changes ? { body: JSON.stringify(changes) } : {}),
  });
  if (!response.ok) {
    throw await settingsResponseError(response);
  }
  const result = await response.json();
  storeLocalPreference("lattice.awake.name.user", result.identity.user_name);
  storeLocalPreference("lattice.awake.name.assistant", result.identity.ai_name);
  storeLocalPreference("lattice.awake.meetingDate", result.identity.meeting_date ?? "");
  window.dispatchEvent(new CustomEvent('lattice:features',{detail:result.features}));
  if(changes)window.dispatchEvent(new CustomEvent('lattice:settings-saved',{detail:result}));
  return result;
}
