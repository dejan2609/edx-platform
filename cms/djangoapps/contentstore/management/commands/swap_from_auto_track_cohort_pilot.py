from course_modes.models import CourseMode
from django.core.management.base import BaseCommand, CommandError
from opaque_keys import InvalidKeyError
from opaque_keys.edx.keys import CourseKey
from opaque_keys.edx.locations import SlashSeparatedCourseKey
from opaque_keys.edx.locator import LibraryUsageLocator

from openedx.core.djangoapps.course_groups.cohorts import CourseCohort
from openedx.core.djangoapps.course_groups.models import (CourseUserGroup, CourseUserGroupPartitionGroup)
from openedx.core.djangoapps.verified_track_content.models import VerifiedTrackCohortedCourse

from xmodule.modulestore import ModuleStoreEnum
from xmodule.modulestore.django import modulestore
from xmodule.partitions.partitions import ENROLLMENT_TRACK_PARTITION_ID


class Command(BaseCommand):
    """
    Migrates a course's xblock's group_access from Verified Track Cohorts to Enrollment Tracks
    """

    def handle(self, *args, **options):
        # TODO convert to config model object configurable via django admin
        course_id = 'course-v1:edX+DemoX+Demo_Course'
        audit_cohort_names = ['Audit Cohort', 'Third Cohort']

        module_store = modulestore()

        try:
            course_key = CourseKey.from_string(course_id)
        except InvalidKeyError:
            try:
                course_key = SlashSeparatedCourseKey.from_string(course_id)
            except InvalidKeyError:
                raise CommandError("Invalid course_key: '%s'." % course_id)

        items = module_store.get_items(course_key)
        if not items:
            raise CommandError("Course with %s key not found." % course_id)

        # Get the CourseUserGroup IDs for the audit course names
        audit_course_user_group_ids = CourseUserGroup.objects.filter(
            name__in=audit_cohort_names,
            course_id=course_key,
            group_type=CourseUserGroup.COHORT,
        ).values_list('id', flat=True)

        # Get all of the audit CourseCohorts from the above IDs that are RANDOM
        random_audit_course_user_group_ids = CourseCohort.objects.filter(
            course_user_group_id__in=audit_course_user_group_ids,
            assignment_type=CourseCohort.RANDOM
        ).values_list('course_user_group_id', flat=True)

        # Get the CourseUserGroupPartitionGroup for the above IDs, these contain the partition IDs and group IDs
        # that are set for group_access inside of modulestore
        random_audit_course_user_group_partition_groups = list(CourseUserGroupPartitionGroup.objects.filter(
            course_user_group_id__in=random_audit_course_user_group_ids
        ))

        # Get the single Verified Track Cohorted Course for the course
        verified_track = VerifiedTrackCohortedCourse.objects.get(course_key=course_key)

        # If there is no verified track, raise an error
        if not verified_track:
            raise CommandError('No VerifiedTrackCohortedCourse found for course: %s' % course_id)

        # Get the single CourseUserGroupPartitionGroup for the verified_track based on the verified_track name
        verified_course_user_group = CourseUserGroup.objects.get(
            course_id=course_key,
            group_type=CourseUserGroup.COHORT,
            name=verified_track.verified_cohort_name
        )
        verified_course_user_group_partition_group = CourseUserGroupPartitionGroup.objects.get(
            course_user_group_id=verified_course_user_group.id
        )

        # Get the enrollment track CourseModes for the course
        audit_course_mode = CourseMode.objects.get(
            course_id=course_key,
            mode_slug=CourseMode.AUDIT
        )
        verified_course_mode = CourseMode.objects.get(
            course_id=course_key,
            mode_slug=CourseMode.VERIFIED
        )
        # Verify that the enrollment track course modes exist
        if not audit_course_mode or not verified_course_mode:
            raise CommandError('Audit or Verified course modes are not defined for course: %s' % course_id)

        for item in items:
            # Checks whether or not an xblock is published, taken from contentstore/views/item.py create_xblock_info
            is_library_block = isinstance(item.location, LibraryUsageLocator)
            published = modulestore().has_published_version(item) if not is_library_block else None

            # Verify that there exists group access for this xblock, otherwise skip these checks
            if item.group_access:
                set_audit_enrollment_track = False
                set_verified_enrollment_track = False

                # Check the partition and group IDs for the audit course groups, if they exist in the xblock's access
                # settings then set the audit track flag to true
                for audit_course_user_group_partition_group in random_audit_course_user_group_partition_groups:
                    audit_partition_group_access = item.group_access.get(
                        audit_course_user_group_partition_group.partition_id,
                        None
                    )
                    if (audit_partition_group_access
                            and audit_course_user_group_partition_group.group_id in audit_partition_group_access):
                        set_audit_enrollment_track = True

                # Check the partition and group IDs for the verified course group, if it exists in the xblock's access
                # settings then set the verified track flag to true
                verified_partition_group_access = item.group_access.get(
                    verified_course_user_group_partition_group.partition_id,
                    None
                )
                if (verified_partition_group_access
                        and verified_course_user_group_partition_group.group_id in verified_partition_group_access):
                    set_verified_enrollment_track = True

                # Add the enrollment track ids to a group access array
                enrollment_track_group_access = []
                if set_audit_enrollment_track:
                    enrollment_track_group_access.append(audit_course_mode.id)
                if set_verified_enrollment_track:
                    enrollment_track_group_access.append(verified_course_mode.id)

                # If either the audit track, or verified track needed to be updated set the access, update and publish
                if set_verified_enrollment_track or set_audit_enrollment_track:
                    # Check that the xblock is published if it needs changes, otherwise raise an error
                    if not published:
                        raise CommandError('XBlock for course, %s needs access changes, but is a draft' % course_id)
                    item.group_access = {ENROLLMENT_TRACK_PARTITION_ID: enrollment_track_group_access}
                    module_store.update_item(item, ModuleStoreEnum.UserID.mgmt_command)
                    module_store.publish(item.location, ModuleStoreEnum.UserID.mgmt_command)
