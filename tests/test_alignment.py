import unittest

from alignment import (
    aligned_sub_frequency, alignment_profile_signature, band_for_frequency,
    normalise_offsets,
)


class AlignmentTests(unittest.TestCase):
    def test_amateur_bands_are_detected_without_affecting_out_of_band_use(self):
        self.assertEqual(band_for_frequency(21_200_000).key, "15m")
        self.assertEqual(band_for_frequency(3_784_980).key, "80m")
        self.assertIsNone(band_for_frequency(4_500_000))

    def test_alignment_is_signed_and_selected_per_band(self):
        offsets = {"80m": -10, "15m": 20}
        self.assertEqual(aligned_sub_frequency(21_200_000, offsets), 21_200_020)
        self.assertEqual(aligned_sub_frequency(3_784_980, offsets), 3_784_970)
        self.assertEqual(aligned_sub_frequency(14_200_000, offsets), 14_200_000)

    def test_persisted_offsets_are_sanitised_and_limited(self):
        self.assertEqual(
            normalise_offsets({"15m": "24", "80m": -2_000, "bogus": 30}),
            {"15m": 20, "80m": -1_000},
        )

    def test_profile_signature_is_ordered_and_case_insensitive(self):
        forward = alignment_profile_signature(
            "two", "Kenwood TS-990S", "COM4", "Kenwood TS-590SG", "COM8"
        )
        same_case = alignment_profile_signature(
            "TWO", "KENWOOD TS-990S", "com4", "kenwood ts-590sg", "com8"
        )
        reverse = alignment_profile_signature(
            "two", "Kenwood TS-590SG", "COM8", "Kenwood TS-990S", "COM4"
        )
        self.assertEqual(forward, same_case)
        self.assertNotEqual(forward, reverse)


if __name__ == "__main__":
    unittest.main()
