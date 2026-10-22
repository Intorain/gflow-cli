@e2e @e2e_video
Feature: A video whose submit reply body is lost is reported as possibly billed
  The video twin of migrated_submit_body_loss.feature. Before #1010 a lost `YhhmEf`
  reply body read as "no reply" and a retryable exit 9 while Flow rendered and billed the
  clip, so the advice to retry bought a second clip.

  Scenario: A text-to-video submit whose reply body is lost is not re-submitted
    Given a video submit whose reply body Chrome cannot return
    When gflow generates a lite clip on the migrated host
    Then exactly one video submit reached Flow
    And gflow reports a timeout that is not retryable and says to check the project
