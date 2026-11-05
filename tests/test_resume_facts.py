import json
import unittest
from unittest.mock import Mock, patch

from selfapply.scoring.tailor import (
    _build_judge_prompt,
    _build_tailor_prompt,
    artifact_prefix,
    assemble_resume_text,
    build_tailoring_plan,
    judge_tailored_resume,
    tailor_resume,
)
from selfapply.scoring.validator import (
    build_allowed_skills,
    remove_unsupported_skills,
    validate_json_fields,
    validate_tailored_resume,
)


class ResumeFactValidationTests(unittest.TestCase):
    def setUp(self):
        self.profile = {
            "personal": {
                "full_name": "Candidate Name",
                "email": "candidate@example.com",
                "github_url": "https://github.com/candidate",
            },
            "skills_boundary": {
                "languages": ["TypeScript"],
                "tools": ["Docker", "PostgreSQL"],
            },
            "resume_facts": {
                "preserved_companies": ["Example Company"],
                "preserved_projects": ["Example Project"],
                "preserved_school": "Example University",
                "experience_entries": [{
                    "company": "Example Company",
                    "header": "Example Company - Software Engineer",
                    "subtitle": "Jan 2020 - Present | Portland, OR",
                }],
                "education_entries": [
                    "Master of Science | Example University - May 2020",
                ],
            },
        }
        self.original = (
            "TypeScript Docker Example Company Example Project "
            "Example University Jan 2020 Portland"
        )

    def _data(self):
        return {
            "title": "Software Engineer",
            "summary": "Built production applications with TypeScript.",
            "skills": {"Languages": "TypeScript", "Tools": "Docker"},
            "experience": [{
                "header": "Software Engineer at Example Company",
                "subtitle": "Jan 2020 - Present | Portland, OR",
                "bullets": ["Built production applications."],
            }],
            "projects": [{
                "header": "Example Project",
                "subtitle": "",
                "bullets": ["Built a local application."],
            }],
            "education": "Example University",
        }

    def test_profile_skill_without_master_resume_evidence_is_rejected(self):
        data = self._data()
        data["skills"]["Databases"] = "PostgreSQL"
        result = validate_json_fields(
            data, self.profile, original_text=self.original,
        )
        self.assertFalse(result["passed"])
        self.assertTrue(any("PostgreSQL" in error for error in result["errors"]))

    def test_generated_unsupported_skill_is_removed_before_validation(self):
        skills, removed = remove_unsupported_skills(
            {"Languages": "TypeScript, Spring", "Tools": "Docker"},
            self.profile,
            original_text=self.original,
        )
        self.assertEqual(skills["Languages"], "TypeScript")
        self.assertEqual(skills["Tools"], "Docker")
        self.assertEqual(removed, ["Spring"])

    def test_learning_target_is_not_allowed_in_technical_skills(self):
        profile = json.loads(json.dumps(self.profile))
        profile["skills_boundary"]["target_learning_skills"] = ["Rust"]
        skills, removed = remove_unsupported_skills(
            {"Languages": "TypeScript, Rust"}, profile,
            original_text=self.original,
        )
        self.assertEqual(skills["Languages"], "TypeScript")
        self.assertEqual(removed, ["Rust"])

    def test_placeholder_dates_are_rejected(self):
        data = self._data()
        data["experience"][0]["subtitle"] = "Tech | Dates"
        result = validate_json_fields(
            data, self.profile, original_text=self.original,
        )
        self.assertFalse(result["passed"])
        self.assertTrue(any("placeholder" in error.lower() for error in result["errors"]))

    def test_resume_without_projects_can_tailor_without_fallback(self):
        profile = json.loads(json.dumps(self.profile))
        profile["resume_facts"]["preserved_projects"] = []
        data = self._data()
        data["projects"] = []
        original = self.original.replace("Example Project ", "")
        result = validate_json_fields(data, profile, original_text=original)
        self.assertTrue(result["passed"], result["errors"])

        text = assemble_resume_text(data, profile)
        full_result = validate_tailored_resume(
            text, profile, original_text=original,
        )
        self.assertTrue(full_result["passed"], full_result["errors"])

    def test_assembly_injects_immutable_experience_and_education(self):
        data = self._data()
        data["experience"][0]["header"] = "Invented Role at Example Company"
        data["experience"][0]["subtitle"] = "Tech | Dates"
        data["education"] = "Incomplete school"
        text = assemble_resume_text(data, self.profile)
        self.assertIn("Example Company - Software Engineer", text)
        self.assertIn("Jan 2020 - Present | Portland, OR", text)
        self.assertIn("Master of Science | Example University - May 2020", text)
        self.assertNotIn("Invented Role", text)
        self.assertNotIn("Tech | Dates", text)
        self.assertNotIn("Incomplete school", text)

    def test_legacy_headers_without_company_do_not_repeat_first_employer(self):
        profile = json.loads(json.dumps(self.profile))
        profile["resume_facts"]["experience_entries"] = [
            {"header": "First Company - Jan 2022 to Present | Engineer"},
            {"header": "Second Company - Jan 2020 to Dec 2021 | Analyst"},
        ]
        data = self._data()
        data["experience"] = [
            {"header": "First Company | Engineer", "subtitle": "", "bullets": ["First."]},
            {"header": "Second Company | Analyst", "subtitle": "", "bullets": ["Second."]},
        ]
        text = assemble_resume_text(data, profile)
        self.assertEqual(text.count("First Company - Jan 2022 to Present"), 1)
        self.assertEqual(text.count("Second Company - Jan 2020 to Dec 2021"), 1)

    def test_full_validator_rejects_malformed_contact_url(self):
        text = assemble_resume_text(self._data(), self.profile).replace(
            "https://github.com/candidate", "\\https://github.com/candidate",
        )
        result = validate_tailored_resume(
            text, self.profile, original_text=self.original,
        )
        self.assertFalse(result["passed"])
        self.assertTrue(any("malformed URL" in error for error in result["errors"]))

    def test_prompt_excludes_profile_skills_absent_from_master_resume(self):
        prompt = _build_tailor_prompt(self.profile, self.original)
        self.assertIn("TypeScript", prompt)
        self.assertIn("Docker", prompt)
        self.assertNotIn("PostgreSQL", prompt)
        self.assertIn("Do not turn an inferred or job-description-only skill", prompt)

    def test_legacy_confirmed_additional_skill_is_ignored(self):
        profile = json.loads(json.dumps(self.profile))
        profile["skills_boundary"]["confirmed_additional_skills"] = ["Kubernetes"]
        allowed = build_allowed_skills(profile, self.original)
        prompt = _build_tailor_prompt(profile, self.original)
        self.assertNotIn("kubernetes", allowed)
        self.assertNotIn("CONFIRMED ADDITIONAL SKILLS", prompt)

    def test_assembly_deterministically_adds_transferable_familiarity(self):
        profile = json.loads(json.dumps(self.profile))
        profile["skills_boundary"]["transferable_familiarity"] = [
            "Golang", "PostgreSQL", "gRPC",
        ]
        data = self._data()
        data["skills"]["Languages"] = "TypeScript, Golang"
        text = assemble_resume_text(data, profile)
        self.assertIn(
            "Databases & Data: PostgreSQL",
            text,
        )
        self.assertIn("Tools: Docker, gRPC", text)
        self.assertNotIn("Related Technologies", text)
        self.assertEqual(text.count("Golang"), 1)
        experience = text.split("EXPERIENCE", 1)[1]
        self.assertNotIn("PostgreSQL", experience)
        self.assertNotIn("gRPC", experience)
        judge_prompt = _build_judge_prompt(profile)
        self.assertIn("PostgreSQL, gRPC", judge_prompt)
        self.assertIn("must not be failed", judge_prompt)

    def test_normal_mode_treats_semantic_judge_as_advisory(self):
        client = Mock()
        client.chat.side_effect = [
            json.dumps(self._data()),
            "VERDICT: FAIL\nSEVERITY: ADVISORY\nISSUES: model misread an extraction artifact",
        ]
        job = {
            "title": "Software Engineer",
            "site": "Example",
            "location": "Portland, OR",
            "full_description": "Build TypeScript applications.",
        }
        with patch("selfapply.scoring.tailor.get_client", return_value=client):
            text, report = tailor_resume(
                self.original, job, self.profile,
                max_retries=0, validation_mode="normal",
            )
        self.assertTrue(text)
        self.assertEqual(report["status"], "approved")
        self.assertIn("extraction artifact", report["judge_advisory"])
        self.assertIn("master_ats_match", report)
        self.assertIn("ats_match", report)
        self.assertIn("overall_score", report["ats_match"])

    def test_normal_mode_blocks_hard_semantic_fabrication(self):
        client = Mock()
        client.chat.side_effect = [
            json.dumps(self._data()),
            (
                "VERDICT: FAIL\nSEVERITY: HARD\n"
                "ISSUES: Invented LLM and RAG pipeline work in an employer bullet."
            ),
        ]
        job = {
            "title": "Software Engineer",
            "site": "Example",
            "location": "Portland, OR",
            "full_description": "Build TypeScript applications.",
        }
        with patch("selfapply.scoring.tailor.get_client", return_value=client):
            text, report = tailor_resume(
                self.original, job, self.profile,
                max_retries=0, validation_mode="normal",
            )
        self.assertTrue(report["fallback_to_master"])
        self.assertNotIn("LLM and RAG pipeline", text)
        self.assertEqual(report["judge"]["severity"], "HARD")

    def test_creative_mode_never_blocks_on_hard_factual_judge(self):
        client = Mock()
        client.chat.side_effect = [
            json.dumps(self._data()),
            (
                "VERDICT: FAIL\nSEVERITY: HARD\n"
                "ISSUES: Added cloud optimization and secure workflow claims."
            ),
        ]
        job = {
            "title": "Cloud Engineer", "site": "Example", "location": "Remote",
            "full_description": "Optimize cloud services and implement secure workflows.",
        }
        with patch("selfapply.scoring.tailor.get_client", return_value=client):
            text, report = tailor_resume(
                self.original, job, self.profile, max_retries=0,
                validation_mode="creative", allow_unverified_claims=True,
            )
        self.assertTrue(text)
        self.assertEqual(report["status"], "approved")
        self.assertTrue(report["unverified_claims_allowed"])
        self.assertIn("cloud optimization", report["judge_advisory"])

    def test_creative_validation_warns_instead_of_blocking_new_domain_skills(self):
        data = self._data()
        data["skills"] = {"Clinical": "Patient care, Epic"}
        result = validate_json_fields(
            data, self.profile, mode="creative", original_text=self.original,
        )
        self.assertTrue(result["passed"], result["errors"])
        self.assertTrue(any("Unsupported skill" in item for item in result["warnings"]))

    def test_failed_judge_without_severity_fails_closed(self):
        client = Mock()
        client.chat.return_value = "VERDICT: FAIL\nISSUES: unsupported work claim"
        with patch("selfapply.scoring.tailor.get_client", return_value=client):
            result = judge_tailored_resume(
                self.original, self.original, "Engineer", self.profile,
            )
        self.assertFalse(result["passed"])
        self.assertEqual(result["severity"], "HARD")

    def test_judge_downgrades_false_missing_skill_claim_only_when_verbatim_in_master(self):
        client = Mock()
        client.chat.return_value = (
            "VERDICT: FAIL\nSEVERITY: HARD\n"
            'ISSUES: The tailored summary includes "Docker", which is not supported '
            "by the original resume."
        )
        with patch("selfapply.scoring.tailor.get_client", return_value=client):
            result = judge_tailored_resume(
                self.original, self.original, "Engineer", self.profile,
            )
        self.assertFalse(result["passed"])
        self.assertEqual(result["severity"], "ADVISORY")
        self.assertIn("present verbatim", result["severity_override"])

    def test_judge_accepts_user_preserved_metric_as_grounded_evidence(self):
        profile = json.loads(json.dumps(self.profile))
        profile["resume_facts"]["real_metrics"] = [
            "11+ years of software engineering experience",
        ]
        client = Mock()
        client.chat.return_value = (
            "VERDICT: FAIL\nSEVERITY: HARD\n"
            'ISSUES: "11+ years of software engineering experience" is not supported '
            "by the original resume."
        )
        with patch("selfapply.scoring.tailor.get_client", return_value=client):
            result = judge_tailored_resume(
                self.original, self.original, "Engineer", profile,
            )
        self.assertEqual(result["severity"], "ADVISORY")

    def test_judge_does_not_downgrade_invented_work_phrase(self):
        client = Mock()
        client.chat.return_value = (
            "VERDICT: FAIL\nSEVERITY: HARD\n"
            'ISSUES: "Architected LLM and RAG pipelines" is not supported by the '
            "original resume."
        )
        with patch("selfapply.scoring.tailor.get_client", return_value=client):
            result = judge_tailored_resume(
                self.original, self.original, "Engineer", self.profile,
            )
        self.assertEqual(result["severity"], "HARD")
        self.assertEqual(result["severity_override"], "")

    def test_judge_accepts_equivalent_generic_software_duration_summary(self):
        original = (
            "SUMMARY\n11+ years of experience building production applications "
            "and distributed software systems."
        )
        client = Mock()
        client.chat.return_value = (
            "VERDICT: FAIL\nSEVERITY: HARD\n"
            'ISSUES: The claim "11+ years of software engineering experience" '
            "is not supported and alters the original context."
        )
        with patch("selfapply.scoring.tailor.get_client", return_value=client):
            result = judge_tailored_resume(original, original, "Engineer", self.profile)
        self.assertTrue(result["passed"])
        self.assertEqual(result["severity"], "NONE")
        self.assertIn("Unchanged duration", result["severity_override"])

    def test_judge_accepts_duration_rephrase_when_issue_also_quotes_original(self):
        original = "11+ years of experience building production applications."
        client = Mock()
        client.chat.return_value = (
            "VERDICT: FAIL\nSEVERITY: HARD\n"
            'ISSUES: "11+ years of software engineering experience" is a rephrasing '
            'of "11+ years of experience building production applications" and could '
            "be interpreted as fabrication or a more specific claim."
        )
        with patch("selfapply.scoring.tailor.get_client", return_value=client):
            result = judge_tailored_resume(original, original, "Engineer", self.profile)
        self.assertTrue(result["passed"])
        self.assertEqual(result["severity"], "NONE")

    def test_judge_does_not_accept_duration_attached_to_new_technology(self):
        original = "11+ years of experience building production software systems."
        client = Mock()
        client.chat.return_value = (
            "VERDICT: FAIL\nSEVERITY: HARD\n"
            'ISSUES: "11+ years of Kubernetes experience" is not supported.'
        )
        with patch("selfapply.scoring.tailor.get_client", return_value=client):
            result = judge_tailored_resume(original, original, "Engineer", self.profile)
        self.assertFalse(result["passed"])
        self.assertEqual(result["severity"], "HARD")

    def test_judge_structurally_excludes_targeted_exposure_section(self):
        client = Mock()
        client.chat.return_value = "VERDICT: PASS\nSEVERITY: NONE\nISSUES: none"
        tailored = (
            "TECHNICAL SKILLS\nLanguages: TypeScript\n\n"
            "TARGETED TECHNICAL EXPOSURE\n"
            "- Rust: Active learning; not prior production experience.\n\n"
            "EXPERIENCE\nExample Company\n- Built applications."
        )
        with patch("selfapply.scoring.tailor.get_client", return_value=client):
            result = judge_tailored_resume(
                self.original, tailored, "Engineer", self.profile,
            )
        submitted = client.chat.call_args.args[0][1]["content"]
        self.assertNotIn("TECHNICAL SKILLS", submitted)
        self.assertNotIn("TARGETED TECHNICAL EXPOSURE", submitted)
        self.assertNotIn("Active learning", submitted)
        self.assertIn("EXPERIENCE", submitted)
        self.assertEqual(
            result["excluded_sections"],
            ["TECHNICAL SKILLS", "TARGETED TECHNICAL EXPOSURE"],
        )

    def test_exposure_section_is_not_parsed_as_technical_skills(self):
        profile = json.loads(json.dumps(self.profile))
        profile["skills_boundary"]["target_learning_skills"] = ["Rust"]
        text = assemble_resume_text(self._data(), profile)
        result = validate_tailored_resume(
            text, profile, original_text=self.original,
        )
        self.assertTrue(result["passed"], result["errors"])
        self.assertIn("TARGETED TECHNICAL EXPOSURE", text)

    def test_exposure_only_starts_near_max_and_is_capped_at_three(self):
        description = (
            "Rust, Kubernetes, Terraform, Kafka, Redis, Scala, and Swift are required. "
            "Build Rust Kubernetes Terraform Kafka Redis Scala Swift systems."
        )
        below = build_tailoring_plan(
            self.original, description, self.profile, 0.85,
        )
        maximum = build_tailoring_plan(
            self.original, description, self.profile, 1.0,
        )
        self.assertEqual(below["planned_exposure_skills"], [])
        self.assertGreater(len(maximum["planned_exposure_skills"]), 0)
        self.assertLessEqual(len(maximum["planned_exposure_skills"]), 3)

    def test_target_aggressiveness_zero_only_requires_matching_master(self):
        # Master and tailored score identically (both drop nothing, add
        # nothing) -- at aggressiveness 0.0 that must be accepted even
        # though it doesn't reach the factual ceiling.
        data = self._data()

        def fake_chat(messages, **kwargs):
            if "resume quality judge" in messages[0]["content"]:
                return "VERDICT: PASS\nISSUES: none"
            return json.dumps(data)

        client = Mock()
        client.chat.side_effect = fake_chat
        job = {
            "title": "Platform Engineer", "site": "Example", "location": "Remote",
            "full_description": "Docker is required. Build Docker production systems.",
        }
        with patch("selfapply.scoring.tailor.get_client", return_value=client):
            text, report = tailor_resume(
                self.original, job, self.profile, max_retries=0,
                validation_mode="strict", target_aggressiveness=0.0,
            )
        self.assertTrue(report["ats_quality"]["passed"])
        self.assertEqual(report["ats_quality"]["target_aggressiveness"], 0.0)
        self.assertAlmostEqual(
            report["ats_quality"]["target_score"],
            report["master_ats_match"]["overall_score"],
            places=1,
        )

    def test_target_aggressiveness_one_reaches_ninety_or_ceiling(self):
        data = self._data()

        def fake_chat(messages, **kwargs):
            if "resume quality judge" in messages[0]["content"]:
                return "VERDICT: PASS\nISSUES: none"
            return json.dumps(data)

        client = Mock()
        client.chat.side_effect = fake_chat
        job = {
            "title": "Platform Engineer", "site": "Example", "location": "Remote",
            "full_description": "Docker is required. Build Docker production systems.",
        }
        with patch("selfapply.scoring.tailor.get_client", return_value=client):
            text, report = tailor_resume(
                self.original, job, self.profile, max_retries=0,
                validation_mode="strict", target_aggressiveness=1.0,
            )
        ceiling = report["ats_quality"]["factual_ceiling_score"]
        expected = min(90.0, ceiling)
        self.assertAlmostEqual(report["ats_quality"]["target_score"], expected, places=1)

    def test_target_aggressiveness_defaults_to_half_when_profile_unset(self):
        data = self._data()

        def fake_chat(messages, **kwargs):
            if "resume quality judge" in messages[0]["content"]:
                return "VERDICT: PASS\nISSUES: none"
            return json.dumps(data)

        client = Mock()
        client.chat.side_effect = fake_chat
        job = {
            "title": "Platform Engineer", "site": "Example", "location": "Remote",
            "full_description": "Docker is required. Build Docker production systems.",
        }
        with patch("selfapply.scoring.tailor.get_client", return_value=client):
            text, report = tailor_resume(
                self.original, job, self.profile, max_retries=0, validation_mode="strict",
            )
        self.assertEqual(report["ats_quality"]["target_aggressiveness"], 0.5)

    def test_maximum_slider_adds_unrelated_jd_tools_only_as_learning_targets(self):
        profile = json.loads(json.dumps(self.profile))
        client = Mock()
        client.chat.return_value = json.dumps(self._data())
        job = {
            "title": "Platform Engineer", "site": "Example", "location": "Remote",
            "full_description": (
                "Kubernetes and Rust are required. Operate Kubernetes services "
                "and build Rust systems."
            ),
        }
        with patch("selfapply.scoring.tailor.get_client", return_value=client):
            text, report = tailor_resume(
                self.original, job, profile, max_retries=0,
                validation_mode="strict", target_aggressiveness=1.0,
            )
        self.assertIn("Cloud & DevOps: kubernetes", text)
        self.assertNotIn("Related Technologies", text)
        self.assertIn("TARGETED TECHNICAL EXPOSURE", text)
        self.assertIn("rust: Active learning and exposure", text)
        self.assertNotIn("built rust", text.casefold())
        self.assertNotIn("operated kubernetes", text.casefold())
        self.assertEqual(report["target_learning_skills"], ["rust"])

    def test_creative_mode_integrates_new_jd_skills_without_extra_section(self):
        profile = json.loads(json.dumps(self.profile))
        profile["skills_boundary"]["allow_unverified_claims"] = True
        profile["skills_boundary"]["target_learning_skills"] = ["Rust"]
        text = assemble_resume_text(self._data(), profile)
        skills = text.split("TECHNICAL SKILLS", 1)[1].split("EXPERIENCE", 1)[0]
        self.assertIn("Rust", skills)
        self.assertNotIn("TARGETED TECHNICAL EXPOSURE", text)
        self.assertNotIn("Related Technologies", text)
        self.assertNotIn("Target Skills", text)

    def test_non_software_skill_never_gets_cloud_devops_label(self):
        text = "SUMMARY\nCandidate\n\nTECHNICAL SKILLS\nCore: Salesforce\n\nEXPERIENCE\n"
        from selfapply.scoring.tailor import _merge_skills_into_technical_categories

        merged = _merge_skills_into_technical_categories(
            text, ["consultative selling", "account expansion"],
        )
        skills = merged.split("TECHNICAL SKILLS", 1)[1].split("EXPERIENCE", 1)[0]
        self.assertIn("Core: Salesforce, consultative selling, account expansion", skills)
        self.assertNotIn("Cloud & DevOps", merged)

    def test_tailoring_is_profile_agnostic_across_three_sample_resumes(self):
        scenarios = [
            {
                "name": "Backend Candidate", "company": "Northwind Labs",
                "role": "Backend Engineer", "school": "State University",
                "project": "Order API", "base_skills": ["Python", "Docker"],
                "new_skill": "Kubernetes",
                "original_bullet": "Built Python APIs for order processing.",
                "tailored_bullet": "Built Python APIs and Kubernetes deployment workflows for order processing.",
            },
            {
                "name": "Frontend Candidate", "company": "Contoso Studio",
                "role": "Frontend Developer", "school": "City College",
                "project": "Design System", "base_skills": ["TypeScript", "Vue.js"],
                "new_skill": "React",
                "original_bullet": "Developed TypeScript interfaces for customers.",
                "tailored_bullet": "Developed React and TypeScript interfaces aligned with reusable design-system patterns.",
            },
            {
                "name": "Data Candidate", "company": "Fabrikam Analytics",
                "role": "Data Engineer", "school": "Technical Institute",
                "project": "Warehouse Pipeline", "base_skills": ["Python", "PostgreSQL"],
                "new_skill": "Spark",
                "original_bullet": "Created Python data pipelines for reporting.",
                "tailored_bullet": "Created Python and Spark data pipelines for distributed reporting workloads.",
            },
        ]
        for index, case in enumerate(scenarios):
            with self.subTest(candidate=case["name"]):
                header = f'{case["company"]} - {case["role"]}'
                subtitle = "Jan 2021 - Present | Remote"
                profile = {
                    "personal": {"full_name": case["name"], "email": f"sample{index}@example.com"},
                    "experience": {"target_role": case["role"]},
                    "skills_boundary": {
                        "tools": case["base_skills"],
                        "skill_expansion_enabled": True,
                        "allow_unverified_claims": True,
                    },
                    "resume_facts": {
                        "preserved_companies": [case["company"]],
                        "preserved_projects": [case["project"]],
                        "preserved_school": case["school"],
                        "experience_entries": [{
                            "company": case["company"], "header": header,
                            "subtitle": subtitle,
                        }],
                        "education_entries": [f'BS | {case["school"]} - 2020'],
                    },
                }
                original = (
                    f'{case["name"]}\n{header}\n{subtitle}\n'
                    f'{case["original_bullet"]}\n{case["project"]}\n'
                    + ", ".join(case["base_skills"])
                    + f'\nBS | {case["school"]} - 2020'
                )
                data = {
                    "title": case["role"],
                    "summary": f'{case["role"]} focused on {case["new_skill"]} delivery.',
                    "skills": {"Core Technologies": case["base_skills"]},
                    "experience": [{
                        "header": header, "subtitle": subtitle,
                        "bullets": [case["tailored_bullet"]],
                    }],
                    "projects": [{
                        "header": case["project"], "subtitle": "",
                        "bullets": [f'Applied {case["new_skill"]} concepts to the target architecture.'],
                    }],
                    "education": f'BS | {case["school"]} - 2020',
                }

                def fake_chat(messages, **kwargs):
                    if "resume quality judge" in messages[0]["content"]:
                        return "VERDICT: PASS\nSEVERITY: NONE\nISSUES: none"
                    return json.dumps(data)

                client = Mock()
                client.chat.side_effect = fake_chat
                job = {
                    "title": case["role"], "site": "Sample Employer",
                    "location": "Remote",
                    "full_description": (
                        f'{case["new_skill"]} is required. Use {case["new_skill"]} '
                        "to build and operate production systems."
                    ),
                }
                with patch("selfapply.scoring.tailor.get_client", return_value=client):
                    text, report = tailor_resume(
                        original, job, profile, max_retries=0,
                        validation_mode="creative", target_aggressiveness=1.0,
                        allow_unverified_claims=True,
                    )
                skills = text.split("TECHNICAL SKILLS", 1)[1].split("EXPERIENCE", 1)[0]
                experience = text.split("EXPERIENCE", 1)[1].split("PROJECTS", 1)[0]
                self.assertIn(case["new_skill"].casefold(), skills.casefold())
                self.assertIn(case["tailored_bullet"], experience)
                self.assertNotIn(case["original_bullet"], experience)
                self.assertNotIn("TARGETED TECHNICAL EXPOSURE", text)
                self.assertNotIn("Related Technologies", text)
                self.assertEqual(report["status"], "approved")

    def test_tailoring_plan_scales_whole_resume_and_requires_skill_review(self):
        job_description = (
            "TypeScript and Rust are required. Build TypeScript services with Rust. "
            "Rust systems and TypeScript APIs support production workflows."
        )
        moderate = build_tailoring_plan(
            self.original, job_description, self.profile, 0.5,
        )
        maximum = build_tailoring_plan(
            self.original, job_description, self.profile, 1.0,
        )
        self.assertEqual(moderate["intensity"]["label"], "Moderate whole-resume tailoring")
        self.assertEqual(maximum["intensity"]["label"], "Maximum whole-resume tailoring")
        self.assertIn("typescript", maximum["supported_skills"])
        self.assertIn("rust", maximum["unsupported_skills"])
        self.assertNotIn("rust", moderate["planned_exposure_skills"])
        self.assertIn("rust", maximum["planned_exposure_skills"])
        self.assertTrue(maximum["warning"])

    def test_creative_moderate_plan_adds_domain_neutral_competencies(self):
        profile = json.loads(json.dumps(self.profile))
        profile["skills_boundary"]["allow_unverified_claims"] = True
        plan = build_tailoring_plan(
            self.original,
            "Manage patient intake, insurance verification, EHR workflows, and referrals.",
            profile,
            0.5,
        )
        self.assertEqual(len(plan["planned_exposure_skills"]), 1)
        self.assertIn(
            plan["planned_exposure_skills"][0],
            {"patient intake", "insurance verification", "ehr workflows", "referrals"},
        )

    def test_confirmed_experience_skill_gets_explicit_prompt_permission(self):
        profile = json.loads(json.dumps(self.profile))
        profile["skills_boundary"]["confirmed_experience_skills"] = ["Rust"]
        prompt = _build_tailor_prompt(profile, self.original, 1.0)
        self.assertIn("USER-CONFIRMED REAL EXPERIENCE SKILLS: Rust", prompt)
        self.assertIn("most relevant existing employer or preserved project bullet", prompt)
        self.assertIn("Maximum whole-resume tailoring (100%)", prompt)

    def test_dropped_supported_skill_is_deterministically_restored(self):
        # The LLM drops "Docker" from the skills section even though it's
        # genuinely supported by the original resume and required by the
        # job. The deterministic safety net should restore it on the first
        # attempt rather than needing a retry or falling back to master.
        data = self._data()
        data["skills"] = {"Languages": "TypeScript"}
        data["summary"] = "Built production applications with TypeScript."

        def fake_chat(messages, **kwargs):
            if "resume quality judge" in messages[0]["content"]:
                return "VERDICT: PASS\nISSUES: none"
            return json.dumps(data)

        client = Mock()
        client.chat.side_effect = fake_chat
        job = {
            "title": "Platform Engineer", "site": "Example", "location": "Remote",
            "full_description": "Docker is required. Build Docker production systems.",
        }
        with patch("selfapply.scoring.tailor.get_client", return_value=client):
            text, report = tailor_resume(
                self.original, job, self.profile,
                max_retries=1, validation_mode="strict",
            )
        self.assertIn("docker", text.casefold())
        self.assertNotEqual(text, self.original)
        self.assertEqual(report["status"], "approved")
        self.assertFalse(report.get("fallback_to_master"))
        self.assertFalse(report.get("used_best_effort_attempt"))
        self.assertTrue(report["ats_quality"]["passed"])
        self.assertEqual(report["attempts"], 1)

    def test_unfixable_ats_regression_falls_back_gracefully(self):
        # Kubernetes is required by the job but supported nowhere -- not in
        # the master resume, not in the profile's skills boundary. No
        # deterministic fix is possible, so every attempt should exhaust
        # retries and the system should still return a valid, safe result
        # (best-effort or master) instead of erroring.
        data = self._data()
        client = Mock()
        client.chat.return_value = json.dumps(data)
        job = {
            "title": "Platform Engineer", "site": "Example", "location": "Remote",
            "full_description": "Kubernetes is required. Operate Kubernetes clusters.",
        }
        with patch("selfapply.scoring.tailor.get_client", return_value=client):
            text, report = tailor_resume(
                self.original, job, self.profile,
                max_retries=1, validation_mode="strict",
            )
        self.assertEqual(report["status"], "fallback_master")
        self.assertTrue(text)

    def test_transferable_fallback_preserves_master_and_improves_match(self):
        profile = json.loads(json.dumps(self.profile))
        profile["skills_boundary"]["tools"].append("Vue.js")
        original = self.original + " Vue.js"
        data = self._data()
        client = Mock()
        client.chat.return_value = json.dumps(data)
        job = {
            "title": "Frontend Engineer", "site": "Example", "location": "Remote",
            "full_description": "React is required. Build React user interfaces.",
        }
        with patch("selfapply.scoring.tailor.get_client", return_value=client):
            text, report = tailor_resume(
                original, job, profile, max_retries=0,
                validation_mode="strict", allow_transferable_skills=True,
            )
        self.assertTrue(report["fallback_to_master"])
        self.assertIn("Frameworks & Libraries: react", text)
        self.assertGreater(
            report["ats_match"]["overall_score"],
            report["master_ats_match"]["overall_score"],
        )

    def test_transferable_similarity_defaults_from_profile_when_not_overridden(self):
        profile = json.loads(json.dumps(self.profile))
        profile["skills_boundary"]["tools"].append("Vue.js")
        profile["skills_boundary"]["transferable_enabled"] = True
        original = self.original + " Vue.js"
        data = self._data()
        client = Mock()
        client.chat.return_value = json.dumps(data)
        job = {
            "title": "Frontend Engineer", "site": "Example", "location": "Remote",
            "full_description": "React is required. Build React user interfaces.",
        }
        with patch("selfapply.scoring.tailor.get_client", return_value=client):
            text, report = tailor_resume(
                original, job, profile, max_retries=0, validation_mode="strict",
            )
        # No explicit allow_transferable_skills/transferable_similarity passed --
        # the profile's saved default should still turn this on.
        self.assertIn("Frameworks & Libraries: react", text)

    def test_high_ats_slider_enables_adjacent_skills_without_separate_checkbox(self):
        profile = json.loads(json.dumps(self.profile))
        profile["skills_boundary"]["tools"].append("Vue.js")
        original = self.original + " Vue.js"
        client = Mock()
        client.chat.return_value = json.dumps(self._data())
        job = {
            "title": "Frontend Engineer", "site": "Example", "location": "Remote",
            "full_description": "React is required. Build React user interfaces.",
        }
        with patch("selfapply.scoring.tailor.get_client", return_value=client):
            text, report = tailor_resume(
                original, job, profile, max_retries=0,
                validation_mode="strict", target_aggressiveness=0.90,
            )
        self.assertIn("Frameworks & Libraries: react", text)
        self.assertTrue(report["transferable_enabled"])
        self.assertEqual(report["minimum_transferable_similarity"], 50)

    def test_single_skill_expansion_checkbox_disables_all_added_jd_skills(self):
        profile = json.loads(json.dumps(self.profile))
        profile["skills_boundary"]["tools"].append("Vue.js")
        profile["skills_boundary"]["skill_expansion_enabled"] = False
        original = self.original + " Vue.js"
        client = Mock()
        client.chat.return_value = json.dumps(self._data())
        job = {
            "title": "Frontend Engineer", "site": "Example", "location": "Remote",
            "full_description": (
                "React and Rust are required. Build React interfaces and Rust services."
            ),
        }
        with patch("selfapply.scoring.tailor.get_client", return_value=client):
            text, report = tailor_resume(
                original, job, profile, max_retries=0,
                validation_mode="strict", target_aggressiveness=1.0,
            )
        self.assertNotIn("Related Technologies", text)
        self.assertNotIn("TARGETED TECHNICAL EXPOSURE", text)
        self.assertFalse(report["transferable_enabled"])
        self.assertEqual(report["target_learning_skills"], [])

    def test_transferable_similarity_off_by_default_without_profile_setting(self):
        profile = json.loads(json.dumps(self.profile))
        data = self._data()
        client = Mock()
        client.chat.return_value = json.dumps(data)
        job = {
            "title": "Frontend Engineer", "site": "Example", "location": "Remote",
            "full_description": "React is required. Build React user interfaces.",
        }
        with patch("selfapply.scoring.tailor.get_client", return_value=client):
            text, report = tailor_resume(
                self.original, job, profile, max_retries=0, validation_mode="strict",
            )
        self.assertNotIn("Transferable Familiarity", text)

    def test_artifact_prefix_distinguishes_identical_titles(self):
        first = {"title": "Software Engineer", "site": "linkedin", "url": "https://jobs/1"}
        second = {"title": "Software Engineer", "site": "linkedin", "url": "https://jobs/2"}
        self.assertNotEqual(artifact_prefix(first), artifact_prefix(second))
        self.assertTrue(artifact_prefix(first).startswith("linkedin_Software_Engineer_"))

    def test_summary_strips_inflated_years_even_without_plus_sign(self):
        profile = json.loads(json.dumps(self.profile))
        profile["resume_facts"]["real_metrics"] = ["11+ years of experience"]
        data = self._data()
        data["summary"] = (
            "Senior Engineer with over 11 years of software engineering "
            "experience building production applications."
        )
        text = assemble_resume_text(data, profile)
        summary_line = text.split("SUMMARY\n")[1].split("\n")[0]
        self.assertNotIn("over 11", summary_line.casefold())
        self.assertIn("11+ years of experience", summary_line)

    def test_summary_years_fix_uses_actual_metric_not_a_hardcoded_number(self):
        profile = json.loads(json.dumps(self.profile))
        profile["resume_facts"]["real_metrics"] = ["12+ years of experience"]
        data = self._data()
        data["summary"] = "Staff Engineer with over 12 years of professional experience."
        text = assemble_resume_text(data, profile)
        summary_line = text.split("SUMMARY\n")[1].split("\n")[0]
        self.assertNotIn("over 12", summary_line.casefold())
        self.assertIn("12+ years of experience", summary_line)

    def test_assembly_never_hardcodes_a_specific_employer_bullet(self):
        profile = json.loads(json.dumps(self.profile))
        profile["resume_facts"]["preserved_companies"] = ["Example Labs"]
        profile["resume_facts"]["real_metrics"] = ["reduced requests from 71 to 21"]
        profile["resume_facts"]["experience_entries"] = [{
            "company": "Example Labs",
            "header": "Example Labs - Software Engineer",
            "subtitle": "Sep 2024 - Present | Berkeley, CA",
        }]
        data = self._data()
        data["experience"] = [{
            "header": "Software Engineer at Example Labs",
            "subtitle": "ignored",
            "bullets": ["Reduced requests from 71 to 21 in an internal workflow."],
        }]
        text = assemble_resume_text(data, profile)
        self.assertIn("Reduced requests from 71 to 21 in an internal workflow.", text)
        self.assertNotIn("Optimized GCP Cloud Logging workflows", text)

    def test_assembly_generically_preserves_omitted_verified_metrics(self):
        profile = json.loads(json.dumps(self.profile))
        profile["resume_facts"]["real_metrics"] = ["reduced requests from 71 to 21"]
        text = assemble_resume_text(self._data(), profile)
        self.assertIn("CAREER HIGHLIGHTS", text)
        self.assertIn("- reduced requests from 71 to 21", text)
        self.assertNotIn("Example Labs", text)


if __name__ == "__main__":
    unittest.main()
