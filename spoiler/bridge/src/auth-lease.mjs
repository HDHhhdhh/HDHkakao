// The host owns the interprocess lease and DPAPI vault; this child never
// touches either. A grant always contains the snapshot read after locking.
export async function coordinatedRefresh({ current, acquire, refresh, persist, release }) {
  const grant = await acquire(current);
  if (!grant || grant.ok !== true || typeof grant.auth_lease_id !== "string" || !grant.auth_lease_id ||
      !grant.credentials || typeof grant.credentials !== "object") {
    throw Object.assign(new Error(), { code: grant?.error_code === "auth_lease_busy" ? "auth_lease_busy" : "auth_lease_invalid" });
  }
  const latest = grant.credentials;
  try {
    if (latest.user_id !== current.user_id || latest.device_uuid !== current.device_uuid) {
      throw Object.assign(new Error(), { code: "auth_identity_changed" });
    }
    // Another app won. Use its durable result without consuming the newly
    // rotated refresh token for a second remote refresh.
    if (latest.oauth_token !== current.oauth_token || latest.refresh_token !== current.refresh_token) return latest;
    if (!latest.refresh_token) throw Object.assign(new Error(), { code: "refresh_unavailable" });
    const refreshed = await refresh(latest);
    if (!refreshed || typeof refreshed.accessToken !== "string" || !refreshed.accessToken) {
      throw Object.assign(new Error(), { code: "invalid_refresh_result" });
    }
    const next = { ...latest, oauth_token: refreshed.accessToken,
      refresh_token: refreshed.refreshToken || latest.refresh_token };
    await persist(next, grant.auth_lease_id);
    return next;
  } finally {
    await release(grant.auth_lease_id);
  }
}
