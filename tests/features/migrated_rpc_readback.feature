@e2e @e2e_auth
Feature: Persisted writes survive an unavailable Chrome response body
  Scenario: Rename and upload recover by independent project readback
    Given a real signed-in Flow browser
    When a rename and an upload commit but their response bodies cannot be read
    Then each write is recovered from persisted project state without resubmission
