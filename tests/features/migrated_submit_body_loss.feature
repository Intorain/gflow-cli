@e2e @e2e_image
Feature: A failure after the submit click never bills a second generation
  Chrome can refuse to return a reply body (#1009). On the image submit reply that
  used to reach the client's retry loop, which ran the whole submit again: one
  `--count 1` run left two images in the project (#1010, spike 2026-10-21).

  Scenario: An image submit whose reply body is lost is reported, not re-submitted
    Given an image submit whose reply body Chrome cannot return
    When gflow generates the image on the migrated host
    Then exactly one image submit reached Flow
    And gflow reports a timeout that is not retryable and says to check the project
