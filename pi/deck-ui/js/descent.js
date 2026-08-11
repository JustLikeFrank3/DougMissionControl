/* descent.js — pure top-of-descent advisory calculations.

   It consumes the same ordered route NAV draws. It never changes that route
   or any aircraft control, and it only treats an altitude as a waypoint
   constraint when the waypoint actually carries one. */

import { distBrg } from './geo.js';

const FT_PER_NM = 6076.12;
const DEFAULTS = {
  targetFt: 3000,
  angleDeg: 3,
  bufferNm: 10,
  approachingNm: 20,
  imminentNm: 5,
  descendingFpm: -200,
  completeFt: 100,
};

function finite(value) { return typeof value === 'number' && Number.isFinite(value); }

function routeDistance(aircraft, route, from, through) {
  var distance = 0;
  var previous = aircraft;
  for (var i = from; i <= through; i++) {
    var waypoint = route[i];
    if (!waypoint || !finite(waypoint.lat) || !finite(waypoint.lon)) return null;
    distance += distBrg(previous.lat, previous.lon, waypoint.lat, waypoint.lon).dist;
    previous = waypoint;
  }
  return distance;
}

function waypointConstraint(waypoint) {
  var value = waypoint && (waypoint.altitude_ft ?? waypoint.constraint_alt_ft);
  return finite(value) && value >= 0 ? value : null;
}

function targetFor(route, activeIndex, currentAltitude, config) {
  if (config.targetMode !== 'manual') {
    for (var i = activeIndex; i < route.length; i++) {
      var altitude = waypointConstraint(route[i]);
      if (altitude !== null && altitude < currentAltitude - config.completeFt) {
        return { index: i, altitudeFt: altitude, source: 'constraint', name: route[i].name };
      }
    }
  }
  var destination = route[route.length - 1];
  return {
    index: route.length - 1,
    altitudeFt: config.targetFt,
    source: config.targetMode === 'manual' ? 'manual' : 'terminal_default',
    name: destination && destination.name,
  };
}

function unavailable(reason) {
  return { state: 'unavailable', reason: reason };
}

export function calculateDescent(input) {
  input = input || {};
  var aircraft = input.aircraft || {};
  var route = Array.isArray(input.route) ? input.route : [];
  var config = Object.assign({}, DEFAULTS, input.config || {});
  var activeIndex = Math.max(0, Math.min(route.length - 1,
    Math.round(input.activeIndex || 0)));

  if (!finite(aircraft.altitudeFt) || !finite(aircraft.lat) || !finite(aircraft.lon))
    return unavailable('aircraft state');
  if (!route.length) return unavailable('route');
  if (input.flightPhase === 'TAKEOFF' || input.flightPhase === 'CLIMB')
    return unavailable('during ' + input.flightPhase.toLowerCase());
  if (!finite(config.targetFt) || config.targetFt < 0 ||
      !finite(config.angleDeg) || config.angleDeg <= 0 || config.angleDeg >= 10 ||
      !finite(config.bufferNm) || config.bufferNm < 0)
    return unavailable('configuration');

  var target = targetFor(route, activeIndex, aircraft.altitudeFt, config);
  var remainingNm = routeDistance(aircraft, route, activeIndex, target.index);
  if (!finite(remainingNm)) return unavailable('route geometry');

  var altitudeToLoseFt = aircraft.altitudeFt - target.altitudeFt;
  var result = {
    state: 'complete',
    reason: null,
    targetAltitudeFt: target.altitudeFt,
    targetName: target.name || 'DEST',
    targetSource: target.source,
    selectedAltitudeFt: finite(aircraft.selectedAltitudeFt)
      ? aircraft.selectedAltitudeFt : null,
    angleDeg: config.angleDeg,
    bufferNm: config.bufferNm,
    altitudeToLoseFt: Math.max(0, altitudeToLoseFt),
    remainingNm: remainingNm,
    descentDistanceNm: 0,
    requiredDistanceNm: 0,
    distanceToTodNm: null,
    timeToTodMin: null,
    requiredVsFpm: null,
    pathAltitudeFt: null,
    pathDeviationFt: null,
    pathStatus: null,
  };
  if (altitudeToLoseFt <= config.completeFt) return result;

  var gradient = Math.tan(config.angleDeg * Math.PI / 180) * FT_PER_NM;
  result.descentDistanceNm = altitudeToLoseFt / gradient;
  result.requiredDistanceNm = result.descentDistanceNm + config.bufferNm;
  result.distanceToTodNm = remainingNm - result.requiredDistanceNm;
  if (finite(aircraft.groundspeedKt) && aircraft.groundspeedKt >= 5) {
    result.timeToTodMin = Math.max(0, result.distanceToTodNm) /
      aircraft.groundspeedKt * 60;
    result.requiredVsFpm = -aircraft.groundspeedKt / 60 * gradient;
  }

  var descending = finite(aircraft.verticalSpeedFpm) &&
    aircraft.verticalSpeedFpm <= config.descendingFpm;
  if (descending) {
    result.state = 'descending';
    result.pathAltitudeFt = target.altitudeFt +
      Math.max(0, remainingNm - config.bufferNm) * gradient;
    result.pathDeviationFt = aircraft.altitudeFt - result.pathAltitudeFt;
    var deviation = Math.abs(result.pathDeviationFt);
    result.pathStatus = deviation < 300 ? 'on_path'
      : deviation <= 750 ? (result.pathDeviationFt > 0 ? 'slightly_high' : 'slightly_low')
      : result.pathDeviationFt > 0 ? 'high' : 'low';
  } else if (result.distanceToTodNm > config.approachingNm) {
    result.state = 'cruise';
  } else if (result.distanceToTodNm > config.imminentNm) {
    result.state = 'approaching';
  } else if (result.distanceToTodNm >= -config.imminentNm) {
    result.state = result.distanceToTodNm > 0 ? 'imminent' : 'tod_now';
  } else {
    result.state = 'missed';
  }
  return result;
}
