"""Conservative, one-to-one rider/vehicle association after model inference."""
import math


class RiderTracker:
    def __init__(self, fps, min_observations=3, max_gap_seconds=.3):
        self.min_observations = min_observations
        self.max_gap = max(1, round(fps * max_gap_seconds))
        self.tracks = {}
        self.sequence = 0
        self.last_index = None

    @staticmethod
    def score(person, vehicle):
        px, py, pr, pb = person['bbox_xyxy']
        vx, vy, vr, vb = vehicle['bbox_xyxy']
        pw, ph, vw, vh = pr-px, pb-py, vr-vx, vb-vy
        if min(pw, ph, vw, vh) <= 0:
            return None
        overlap = max(0, min(pr, vr)-max(px, vx))/min(pw, vw)
        # Rider must sit above/within the cycle, not walk alongside it.
        if (overlap < .65 or py > vy+.15*vh or pb < vy+.15*vh
                or pb > vb+.25*vh or abs((px+pr-vx-vr)/2) > .5*vw):
            return None
        return overlap - .3*abs((px+pr-vx-vr)/2)/vw - .15*abs(pb-vb)/max(ph, vh)

    def update(self, vehicles, index):
        if self.last_index is not None and index <= self.last_index:
            self.tracks.clear()
        self.last_index = index
        self.tracks = {k:t for k,t in self.tracks.items() if index-t['last'] <= self.max_gap}
        persons = [v for v in vehicles if v['class_name']=='person' and v.get('object_id')]
        cycles = [v for v in vehicles if v['class_name'] in {'motorcycle','bicycle'} and v.get('object_id')]
        pairs = []
        for p in persons:
            for v in cycles:
                score = self.score(p, v)
                if score is not None:
                    pairs.append((score, p, v))
        groups, used_tracks = [], set()
        for score, p, v in sorted(pairs, key=lambda pair:pair[0], reverse=True):
            # Both sides must have an unambiguous best match; do not guess in crowds.
            rivals = [s for s,pp,vv in pairs if (pp is p or vv is v) and not (pp is p and vv is v)]
            if rivals and score-max(rivals) < .08:
                continue
            center = v['bottom_center']
            box = v['bbox_xyxy']
            size = max(box[2]-box[0], box[3]-box[1], 1)
            matches = []
            for key, t in self.tracks.items():
                if key in used_tracks:
                    continue
                same_person = p['object_id']==t['rider_id']
                same_vehicle = v['object_id']==t['vehicle_id']
                distance = math.dist(center, t['point'])
                if (same_person or same_vehicle) and distance <= max(20, size*.6) and .5 <= size/t['size'] <= 2:
                    matches.append((key,t))
            if len(matches)>1:
                continue
            if matches:
                key, t = matches[0]
            else:
                self.sequence += 1
                key = f'rider-group-{self.sequence:06d}'
                t = {'count':0}
                self.tracks[key] = t
            t.update(count=t['count']+1, last=index, rider_id=p['object_id'],
                     vehicle_id=v['object_id'], point=list(center), size=size)
            used_tracks.add(key)
            union = [min(p['bbox_xyxy'][0],box[0]), min(p['bbox_xyxy'][1],box[1]),
                     max(p['bbox_xyxy'][2],box[2]), max(p['bbox_xyxy'][3],box[3])]
            groups.append({'object_id':key, 'rider_id':p['object_id'], 'vehicle_id':v['object_id'],
                           'class_name':v['class_name'], 'bbox_xyxy':union,
                           'vehicle_bbox_xyxy':list(box), 'bottom_center':list(center),
                           'status':'confirmed' if t['count']>=self.min_observations else 'pending',
                           'support_frames':t['count'], 'association_score':round(score,4)})
        return groups


def attach_riders(record, tracker):
    """Keep raw detections; publish a separate canonical input for rules."""
    obs = record['observations']
    groups = tracker.update(obs['vehicles'], record['frame']['index'])
    obs['rider_groups'] = groups
    confirmed = {g['vehicle_id']:g for g in groups if g['status']=='confirmed'}
    rule_vehicles = []
    for vehicle in obs['vehicles']:
        item = dict(vehicle)
        group = confirmed.get(vehicle.get('object_id'))
        if group:
            item.update(object_id=group['object_id'], source_object_id=group['vehicle_id'],
                        rider_id=group['rider_id'], rider_group=group)
        rule_vehicles.append(item)
    obs['rule_vehicles'] = rule_vehicles
    record.setdefault('postprocessing', {})['rider_association'] = {'enabled':True, 'version':1}
    return record


def draw_rider_groups(frame, groups):
    import cv2
    for group in groups:
        confirmed = group['status']=='confirmed'
        color = (70, 240, 70) if confirmed else (0, 190, 255)
        x1,y1,x2,y2 = [int(v) for v in group['bbox_xyxy']]
        cv2.rectangle(frame, (x1,y1), (x2,y2), color, 3)
        label = f"{group['object_id']} {group['status'].upper()}"
        members = f"R:{group['rider_id']} + V:{group['vehicle_id']}"
        for offset, text in enumerate([label, members]):
            size, baseline = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, .5, 1)
            x = max(0, min(x1, frame.shape[1]-size[0]-6))
            y = max(18+offset*21, y1-30+offset*21)
            cv2.rectangle(frame, (x,y-size[1]-3), (x+size[0]+4,y+baseline), (20,20,20), -1)
            cv2.putText(frame, text, (x+2,y), cv2.FONT_HERSHEY_SIMPLEX, .5, color, 1, cv2.LINE_AA)
        cv2.circle(frame, tuple(int(v) for v in group['bottom_center']), 5, color, -1)
    return frame
