import unittest
import sys
import types

playwright_module = types.ModuleType("playwright")
playwright_async_api = types.ModuleType("playwright.async_api")
playwright_async_api.TimeoutError = TimeoutError
playwright_async_api.async_playwright = lambda: None
playwright_module.async_api = playwright_async_api
sys.modules.setdefault("playwright", playwright_module)
sys.modules.setdefault("playwright.async_api", playwright_async_api)

import swarm


class SwarmTailoringTests(unittest.TestCase):
    def setUp(self) -> None:
        self.profile = {
            "first_name": "Elijah",
            "last_name": "Wallis",
            "address_line1": "3201 Wynwood Dr",
            "pitch": "Exact profile pitch.",
            "twic": "Active TWIC holder",
            "sea_days_documented": 250,
            "uscg_coursework_completed": ["STCW Basic", "RFPNW"],
            "tankerman_pic_lg_dl_finish_date": "March 6, 2026",
            "mmc_submission_timing": "Full MMC package submits to USCG same week",
            "deployment_readiness": "Immediate deployment ready",
        }

    def test_build_applicant_summary_includes_core_credentials(self) -> None:
        summary = swarm.build_applicant_summary(self.profile)

        self.assertIn("Active TWIC holder", summary)
        self.assertIn("250 documented sea days", summary)
        self.assertIn("STCW Basic", summary)
        self.assertIn("RFPNW", summary)
        self.assertIn("March 6, 2026", summary)
        self.assertIn("Immediate deployment ready", summary)

    def test_build_target_profile_preserves_pitch_and_adds_tailored_fields(self) -> None:
        target = {"company": "Weeks Marine", "url": "https://kiewitcareers.kiewit.com/Weeks"}

        tailored = swarm.build_target_profile(self.profile, target)

        self.assertEqual(tailored["pitch"], "Exact profile pitch.")
        self.assertEqual(tailored["address"], "3201 Wynwood Dr")
        self.assertIn("Weeks Marine", tailored["career_goals"])
        self.assertIn("Weeks Marine", tailored["work_environment"])
        self.assertIn("Weeks Marine", tailored["cover_letter"])
        self.assertIn("field engineer", " ".join(tailored["job_keywords"]).lower())

    def test_should_skip_request_submit_for_upload_heavy_pages(self) -> None:
        self.assertTrue(
            swarm.should_skip_request_submit(
                "https://jobs.ourcareerpages.com/jobapplication/958904",
                has_file_inputs=True,
            )
        )
        self.assertTrue(
            swarm.should_skip_request_submit(
                "https://vikingdredging.applicantpro.com/jobs/",
                has_file_inputs=False,
            )
        )
        self.assertFalse(
            swarm.should_skip_request_submit(
                "https://curtinmaritime.bamboohr.com/careers/215",
                has_file_inputs=False,
            )
        )


if __name__ == "__main__":
    unittest.main()
