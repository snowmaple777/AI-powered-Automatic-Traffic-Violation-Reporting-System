import unittest
from unittest.mock import Mock, patch

import numpy as np

from model_library.base import FrameContext, ModelResult
from model_library.perception import OCRModel
from model_library.perception_core import PlateOCRTracker
from rider_tracking import RiderTracker, attach_riders, draw_rider_groups


class PlatePersistenceTests(unittest.TestCase):
    def test_only_strictly_higher_valid_confidence_replaces_text(self):
        tracker = PlateOCRTracker()
        tracker.update(1, 0, 'ABC1234', .90, veh_cls=2)
        tracker.update(1, 1, 'ABC1234', .90, veh_cls=2)
        self.assertTrue(tracker.records[1]['confirmed'])
        for frame, text, score in [(2, 'DEF5678', .80), (3, 'DEF5678', .90),
                                   (4, None, 0), (5, 'X', .99),
                                   (6, 'DEF5678', float('nan'))]:
            tracker.update(1, frame, text, score, veh_cls=2)
            self.assertEqual(tracker.get_plate(1), ('ABC-1234', .90))
        tracker.update(1, 7, 'DEF5678', .95, veh_cls=2)
        self.assertEqual(tracker.get_plate(1), ('DEF-5678', .95))
        self.assertEqual(tracker.get_plate_raw(1), ('DEF5678', .95))

    def test_track_merge_keeps_text_and_its_score_together(self):
        for source_score, expected in [(.80, ('ABC-1234', .90)),
                                       (.90, ('ABC-1234', .90)),
                                       (.95, ('DEF-5678', .95))]:
            tracker = PlateOCRTracker()
            tracker.update(2, 0, 'ABC1234', .90, veh_cls=3)
            tracker.update(1, 1, 'DEF5678', source_score, veh_cls=0)
            tracker.merge_tracks(1, 2)
            self.assertEqual(tracker.get_plate(2), expected)
            self.assertNotIn(1, tracker.records)

    def test_vehicle_keeps_plate_through_long_plate_detection_gap(self):
        with patch('model_library.perception.ONNXPPOCRv6Recognizer') as backend:
            backend.return_value.predict.return_value = ('ABC1234', .90)
            model = OCRModel('unused.onnx', min_width=16)
        frame = np.zeros((100, 200, 3), dtype=np.uint8)
        vehicle = dict(track_id=1, class_id=2, object_id='vehicle-000001')
        plate = dict(track_id=1, vehicle_class_id=2, object_id='vehicle-000001',
                     bbox_xyxy=[10, 10, 90, 40], held=False)

        def infer(index, vehicles, plates=(), mappings=None):
            return model.infer(frame, FrameContext(index, index / 30), {
                'vehicles': ModelResult(vehicles, {'track_mappings': mappings or {}}),
                'plates': ModelResult(list(plates)),
            })

        result = infer(0, [vehicle], [plate])
        self.assertEqual(result.records[0]['text'], 'ABC-1234')
        for index in range(1, 201):
            result = infer(index, [vehicle])
            self.assertEqual(result.artifacts['vehicle_plate_map'],
                             {'vehicle-000001': 'ABC-1234'})
            self.assertEqual(result.artifacts['vehicle_plate_confidence_map'],
                             {'vehicle-000001': .90})
        other = dict(vehicle, track_id=2, object_id='vehicle-000002')
        self.assertNotIn(other['object_id'], infer(201, [vehicle, other]).artifacts['vehicle_plate_map'])
        # A vehicle class change must not inherit an unrelated vehicle's label.
        self.assertFalse(infer(202, [dict(vehicle, class_id=3)]).artifacts['vehicle_plate_map'])
        infer(203, [vehicle], [plate])
        for index in range(204, 296):
            infer(index, [])
        self.assertFalse(infer(296, [vehicle]).artifacts['vehicle_plate_map'])
        infer(297, [vehicle], [plate])
        model.reset()
        self.assertFalse(infer(0, [vehicle]).artifacts['vehicle_plate_map'])

    def test_rider_group_overlay_includes_vehicle_plate(self):
        group = dict(object_id='group-1', vehicle_id='vehicle-1', rider_id='rider-1',
                     status='confirmed', bbox_xyxy=[0, 30, 100, 100], bottom_center=[50, 100])
        tracker = Mock(spec=RiderTracker)
        tracker.update.return_value = [group]
        record = {'frame': {'index': 1}, 'observations': {'vehicles': [
            {'object_id': 'vehicle-1', 'plate_text': 'ABC-1234'}]}}
        attach_riders(record, tracker)
        with patch('cv2.putText') as put_text:
            draw_rider_groups(np.zeros((200, 500, 3), dtype=np.uint8), [group])
        self.assertTrue(any('ABC-1234' in call.args[1] for call in put_text.call_args_list))


if __name__ == '__main__':
    unittest.main()
