// Pure status-rendering regression: no browser, server or original data needed.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, '../app/web/app.js'), 'utf8');
const start = source.indexOf('  const validityOf =');
const end = source.indexOf('  // display units', start);
assert(start >= 0 && end > start);
const helpers = new Function('fmtNum', 'esc', source.slice(start, end) + '\nreturn {statusSummary, validityBadge, offTrackDetails};')(
  (value, places = 0) => Number(value).toFixed(places), value => String(value));
const overlap = {valid: false, pace_eligible: false, validity: {status: 'invalid', label: 'Game invalid recorded',
  off_track: true, off_track_events: 2, off_track_s: 1.25, track_limits_source: 'ac.NumberOfTyresOut',
  track_limits_coverage: 'complete', penalty_observed: true}};
const summary = helpers.statusSummary([overlap]);
assert(summary.includes('1 laps with off-track observed'));
assert(summary.includes('1 laps with game penalty'));
assert(summary.includes('counts can overlap'));
const badge = helpers.validityBadge(overlap);
assert(badge.includes('2 off-track events'));
assert(badge.includes('1.25 s'));
assert(badge.includes('game penalty observed'));
const legacy = {valid: null, validity: {status: 'unverified', off_track: null, off_track_events: null,
  off_track_s: null, track_limits_source: null, track_limits_coverage: 'unavailable'}};
assert(helpers.statusSummary([legacy]).includes('off-track observations unavailable'));
assert(helpers.offTrackDetails(legacy).includes('unavailable'));
assert(!helpers.offTrackDetails(legacy).includes('0 off-track'));
console.log('PASS independent off-track / penalty counts, per-lap counts / duration, and unknown observations');
