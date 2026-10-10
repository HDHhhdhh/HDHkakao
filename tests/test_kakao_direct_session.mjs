import assert from "node:assert/strict";
import test from "node:test";
import { sessionAcquireError } from "../bridge/kakao_direct_session.mjs";
import { authFailure, responseError } from "../bridge/kakao_direct_bridge.mjs";
import { sendWriteOnce } from "../bridge/kakao_direct_send_once.mjs";

test("network login failures are recoverable before WRITE and do not refresh OAuth", () => {
  const original = Object.assign(new Error("PRIVATE TOKEN MESSAGE"), {
    code:"login_failed", cause:Object.assign(new Error("SECRET"), {code:"ECONNRESET"}),
  });
  assert.equal(authFailure(original),false);
  const response = responseError(sessionAcquireError(original));
  assert.equal(response.send_phase,"prewrite");
  assert.equal(response.retryable_prewrite,true);
  assert.equal(response.cause_code,"ECONNRESET");
  assert.equal(response.ambiguous,false);
  assert.ok(!JSON.stringify(response).includes("SECRET"));
  assert.ok(!JSON.stringify(response).includes("PRIVATE"));
});

test("real token expiry still qualifies for refresh", () => {
  assert.equal(authFailure({code:"login_failed",cause:{code:"invalid_access_token",serverStatus:-950}}),true);
});

test("generic profile errors do not rotate otherwise valid credentials", () => {
  for (const error of [
    { code: "profile_request_failed" },
    { code: "profile_request_failed", httpStatus: 500 },
    { code: "get_profile_failed", cause: { code: "ECONNRESET" } },
    { code: "get_profile_failed", cause: { code: "ETIMEDOUT" } },
  ]) assert.equal(authFailure(error), false);
  for (const status of [401, 403]) {
    assert.equal(authFailure({ code: "profile_request_failed", httpStatus: status }), true);
  }
  assert.equal(authFailure({ code: "get_profile_failed", cause: { code: "token_expired" } }), true);
});

test("auth rejection, provider status and permanent local errors never loop", () => {
  for(const original of [
    {code:"login_rejected",serverStatus:-777}, {code:"refresh_rejected",serverStatus:-950},
    {code:"rate_limited",serverStatus:429}, {code:"login_failed",cause:{code:"EACCES"}},
    {code:"login_failed",cause:{code:"login_rejected",serverStatus:-1}},
    {code:"PRIVATE SECRET"}, new Error("PRIVATE TOKEN"),
  ]) {
    const response=responseError(sessionAcquireError(original));
    assert.equal(response.send_phase,"prewrite");
    assert.equal(response.retryable_prewrite,false);
    assert.equal(response.ambiguous,false);
    assert.ok(!JSON.stringify(response).includes("PRIVATE"));
  }
});

test("structured server cause remains separate from send outcome status", () => {
  const response=responseError(sessionAcquireError({code:"login_rejected",serverStatus:-1}));
  assert.equal(response.cause_status_code,-1);
  assert.equal(response.status_code,undefined);
  assert.equal(response.kind,"explicit");
});

test("terminal cause is retained when a deeper cause is transient", () => {
  const response=responseError(sessionAcquireError({
    code:"login_rejected",serverStatus:-777,cause:{code:"ECONNRESET"},
  }));
  assert.equal(response.retryable_prewrite,false);
  assert.equal(response.cause_code,"login_rejected");
  assert.equal(response.cause_status_code,-777);
});

test("an identical network error after WRITE has no prewrite retry permission", async () => {
  let calls=0;
  try {
    await sendWriteOnce({sendMessage:async()=>{calls++;throw Object.assign(new Error("SECRET"),{code:"ECONNRESET"});}},
      {fromString:value=>value},"123","PRIVATE");
    assert.fail("expected failure");
  } catch(error) {
    const response=responseError(error);
    assert.equal(response.kind,"unknown");
    assert.equal(response.send_phase,undefined);
    assert.equal(response.retryable_prewrite,undefined);
  }
  assert.equal(calls,1);
});
