import {statSync, writeFileSync} from 'node:fs';
const {TURSO_PLATFORM_TOKEN: token, TURSO_ORGANIZATION: org,
  TURSO_CONFIRMED_PLAN: expectedPlan, TURSO_CONFIRMED_QUOTA_BYTES: configuredQuota} = process.env;
const bytes = statSync(process.argv[2]).size;
const quota = Number(configuredQuota);
if (!token || !org || !expectedPlan || !Number.isSafeInteger(quota) || quota <= 0 || quota > 10_000_000_000)
  throw new Error('Verified organization, plan and storage quota (at most owner limit 10 GB) are required');
if (bytes >= 4_750_000_000) throw new Error('Packed storage guard exceeded');
async function get(path) {
  const r = await fetch(`https://api.turso.tech/v1/organizations/${encodeURIComponent(org)}/${path}`,
    {headers: {Authorization: `Bearer ${token}`}});
  if (!r.ok) throw new Error(`Turso capacity lookup failed: ${r.status}`);
  return r.json();
}
const [subscription, usage] = await Promise.all([get('subscription'), get('usage')]);
const plan = subscription.subscription?.plan;
const used = Number(usage.total?.storage_bytes ?? usage.organization?.usage?.storage_bytes);
if (plan !== expectedPlan || !Number.isSafeInteger(used) || used < 0)
  throw new Error('Confirmed plan does not match the current account, or usage is unavailable');
const reserve = Math.max(500_000_000, Math.ceil(bytes * 0.1));
const report = {organization: org, plan, quota, used, remaining: quota - used, candidate: bytes, reserve};
console.log(JSON.stringify(report));
if (used + bytes + reserve > quota) throw new Error('Insufficient capacity for old and new databases plus reserve');
writeFileSync('capacity-report.json', JSON.stringify(report, null, 2));
