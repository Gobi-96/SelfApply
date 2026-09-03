from selfapply.scoring.ats_matcher import analyze_ats_match, extract_job_keywords


def test_ats_match_separates_safe_and_unsupported_keyword_gaps():
    job = """
    Senior Backend Engineer
    Python, SQL, AWS, and Docker are required. Kubernetes is preferred.
    Build reliable distributed services and APIs in a collaborative environment.
    """
    master = """
    SUMMARY
    Backend engineer using Python, SQL, AWS, and Docker.
    EXPERIENCE
    Built reliable APIs and distributed services.
    EDUCATION
    Bachelor of Technology
    TECHNICAL SKILLS
    Python, SQL, AWS, Docker
    """
    tailored = """
    SUMMARY
    Backend engineer using Python and Docker.
    EXPERIENCE
    Built reliable APIs.
    EDUCATION
    Bachelor of Technology
    TECHNICAL SKILLS
    Python, Docker
    """
    result = analyze_ats_match(tailored, job, master_resume_text=master)

    assert 0 < result["overall_score"] < 100
    assert "aws" in result["injectable_keywords"]
    assert "sql" in result["injectable_keywords"]
    assert "kubernetes" in result["unsupported_keywords"]
    assert result["sub_scores"]["section_completeness"] == 100.0
    assert result["factual_ceiling_score"] < 100.0
    assert result["target_score"] == result["factual_ceiling_score"]
    assert result["supported_term_retention"] < 100.0
    assert "employer ATS systems" in result["disclaimer"]


def test_keyword_extraction_finds_required_and_preferred_skills():
    result = extract_job_keywords(
        "Python and PostgreSQL are required. Kubernetes is a preferred skill.",
    )
    assert "python" in result["required_skills"]
    assert "postgresql" in result["required_skills"]
    assert "kubernetes" in result["preferred_skills"]


def test_keyword_extraction_caps_long_job_ad_noise():
    repeated = " ".join(f"domain{i} domain{i}" for i in range(30))
    result = extract_job_keywords(f"Python is required. {repeated}")
    assert len(result["keywords"]) == 12
    assert "python" in result["skills"]


def test_short_skill_does_not_match_inside_unrelated_word():
    result = analyze_ats_match(
        "SUMMARY\nOngoing delivery.\nEXPERIENCE\nTesting\nEDUCATION\nBS\nSKILLS\nPython",
        "Go is required for backend services.",
    )
    assert "go" in result["missing_keywords"]


def test_employer_name_is_never_counted_as_a_missing_skill():
    jd = """
    Thomson Reuters is hiring. Join Thomson Reuters today. At Thomson Reuters
    we offer competitive pay, flexible time off, and health insurance. We
    require TypeScript and Python. Legal decisions about recruitment at
    Thomson Reuters are made fairly regardless of protected characteristics.
    """
    resume = "SUMMARY\nEngineer with TypeScript and Python.\nEXPERIENCE\nBuilt things.\nEDUCATION\nBS"
    without_company = extract_job_keywords(jd)
    assert "thomson" in without_company["keywords"]
    with_company = extract_job_keywords(jd, company_name="Thomson Reuters")
    assert "thomson" not in with_company["keywords"]
    assert "reuters" not in with_company["keywords"]

    result = analyze_ats_match(resume, jd, company_name="Thomson Reuters")
    assert "thomson" not in result["missing_keywords"]
    assert "reuters" not in result["missing_keywords"]


def test_generic_benefits_boilerplate_is_not_treated_as_a_skill_requirement():
    jd = """
    We offer competitive pay, flexible paid time off, health insurance, and a
    diverse and inclusive culture. Python is required.
    """
    result = extract_job_keywords(jd)
    for noise in ("competitive", "flexible", "paid", "health", "diversity", "inclusion"):
        assert noise not in result["keywords"]
    assert "python" in result["skills"]


def test_empty_job_description_returns_zero_keyword_and_skill_scores():
    result = analyze_ats_match("SUMMARY\nEngineer", "")
    assert result["sub_scores"]["keyword_match"] == 0.0
    assert result["sub_scores"]["skills_coverage"] == 0.0


def test_non_english_input_is_explicitly_warned():
    result = analyze_ats_match("履歴書 経験 技術 教育", "開発者 技術 経験 必須")
    assert "optimized for English" in result["language_warning"]


def test_non_technical_resume_is_not_capped_by_software_skill_vocabulary():
    resume = """
    SUMMARY
    Registered nurse focused on patient care and clinical documentation.
    EXPERIENCE
    Delivered patient care and maintained clinical documentation.
    EDUCATION
    Bachelor of Science in Nursing
    SKILLS
    Patient care, clinical documentation
    """
    jd = """
    Provide patient care and clinical documentation. Patient care requires
    communication, and clinical documentation supports communication.
    """
    result = analyze_ats_match(resume, jd)
    assert result["skills"] == []
    assert result["competency_terms"]
    assert result["sub_scores"]["skills_coverage"] >= 80.0
    assert result["overall_score"] > 75


def test_domain_neutral_requirement_lists_are_extracted():
    cases = {
        "marketing": (
            "SEO, HubSpot, email marketing, campaign analytics, and audience segmentation are required.",
            "email marketing",
        ),
        "accounting": (
            "Prepare GAAP financial statements, journal entries, bank reconciliations, and variance analysis.",
            "gaap financial statements",
        ),
        "operations": (
            "Own demand forecasting, KPI dashboards, Lean process improvement, and supplier performance.",
            "demand forecasting",
        ),
        "healthcare": (
            "Manage patient intake, insurance verification, EHR workflows, HIPAA compliance, and referrals.",
            "patient intake",
        ),
        "sales": (
            "Drive SaaS pipeline, consultative selling, account expansion, CRM forecasting, and executive presentations.",
            "saas pipeline",
        ),
    }
    for domain, (jd, expected) in cases.items():
        extracted = extract_job_keywords(jd)
        assert expected in extracted["keywords"], (domain, extracted)


def test_non_software_tailoring_terms_raise_the_estimated_match():
    jd = (
        "Manage patient intake, insurance verification, EHR workflows, "
        "HIPAA compliance, referrals, and prior authorizations."
    )
    master = "SUMMARY\nCoordinator\nSKILLS\nScheduling\nEXPERIENCE\nClinic work\nEDUCATION\nBS"
    tailored = (
        "SUMMARY\nHealthcare coordinator\nSKILLS\nPatient intake, insurance verification, "
        "EHR workflows, HIPAA compliance\nEXPERIENCE\nManaged referrals and prior authorizations.\n"
        "EDUCATION\nBS"
    )
    assert analyze_ats_match(tailored, jd)["overall_score"] > analyze_ats_match(master, jd)["overall_score"]
