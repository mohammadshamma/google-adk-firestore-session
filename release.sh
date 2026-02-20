#!/usr/bin/env bash
set -euo pipefail

# Usage: ./release.sh <version>
# Example: ./release.sh 0.2.0

if [ $# -ne 1 ]; then
  echo "Usage: ./release.sh <version>"
  echo "Example: ./release.sh 0.2.0"
  exit 1
fi

VERSION="$1"

# Validate semver-like format (e.g. 0.2.0, 1.0.0, 1.2.3)
if ! echo "$VERSION" | grep -qE '^[0-9]+\.[0-9]+\.[0-9]+$'; then
  echo "Error: Version must be in semver format (e.g. 0.2.0)"
  exit 1
fi

# Read current version from pyproject.toml
CURRENT=$(grep -oE '^version = "([0-9]+\.[0-9]+\.[0-9]+)"' pyproject.toml | grep -oE '[0-9]+\.[0-9]+\.[0-9]+')
if [ -z "$CURRENT" ]; then
  echo "Error: Could not read current version from pyproject.toml"
  exit 1
fi

IFS='.' read -r CUR_MAJOR CUR_MINOR CUR_PATCH <<< "$CURRENT"
IFS='.' read -r NEW_MAJOR NEW_MINOR NEW_PATCH <<< "$VERSION"

# Validate the new version is a single-increment bump:
#   major bump: major+1, minor=0, patch=0
#   minor bump: same major, minor+1, patch=0
#   patch bump: same major, same minor, patch+1
VALID=false
if [ "$NEW_MAJOR" -eq $((CUR_MAJOR + 1)) ] && [ "$NEW_MINOR" -eq 0 ] && [ "$NEW_PATCH" -eq 0 ]; then
  VALID=true  # major bump
elif [ "$NEW_MAJOR" -eq "$CUR_MAJOR" ] && [ "$NEW_MINOR" -eq $((CUR_MINOR + 1)) ] && [ "$NEW_PATCH" -eq 0 ]; then
  VALID=true  # minor bump
elif [ "$NEW_MAJOR" -eq "$CUR_MAJOR" ] && [ "$NEW_MINOR" -eq "$CUR_MINOR" ] && [ "$NEW_PATCH" -eq $((CUR_PATCH + 1)) ]; then
  VALID=true  # patch bump
fi

if [ "$VALID" = false ]; then
  echo "Error: Version $VERSION is not a valid increment from $CURRENT"
  echo "Valid next versions:"
  echo "  $((CUR_MAJOR + 1)).0.0  (major)"
  echo "  $CUR_MAJOR.$((CUR_MINOR + 1)).0  (minor)"
  echo "  $CUR_MAJOR.$CUR_MINOR.$((CUR_PATCH + 1))  (patch)"
  exit 1
fi

# Ensure working tree is clean
if [ -n "$(git status --porcelain)" ]; then
  echo "Error: Working tree is not clean. Commit or stash changes first."
  exit 1
fi

# Ensure we're on the main branch
BRANCH=$(git rev-parse --abbrev-ref HEAD)
if [ "$BRANCH" != "main" ]; then
  echo "Error: Must be on the main branch (currently on '$BRANCH')"
  exit 1
fi

# Ensure main is up to date
git fetch origin main
LOCAL=$(git rev-parse HEAD)
REMOTE=$(git rev-parse origin/main)
if [ "$LOCAL" != "$REMOTE" ]; then
  echo "Error: Local main is not up to date with origin/main. Run 'git pull' first."
  exit 1
fi

RELEASE_BRANCH="release/v$VERSION"

echo ""
echo "Will create release v$VERSION:"
echo "  - Create branch '$RELEASE_BRANCH' with version bump"
echo "  - Open a PR and merge it to main"
echo "  - Tag the merge commit as v$VERSION"
echo "  - Push the tag"
echo ""
read -rp "Proceed? [y/N] " confirm
if [[ "$confirm" != [yY] ]]; then
  echo "Aborted."
  exit 1
fi

# Create release branch and commit version bump
git checkout -b "$RELEASE_BRANCH"
sed -i.bak "s/^version = \".*\"/version = \"$VERSION\"/" pyproject.toml
rm -f pyproject.toml.bak
git add pyproject.toml
git commit -m "Bump version to $VERSION"
git push origin "$RELEASE_BRANCH"

# Create PR and merge it
PR_URL=$(gh pr create --title "Bump version to $VERSION" --body "Release v$VERSION" --base main --head "$RELEASE_BRANCH")
echo "Created PR: $PR_URL"
gh pr merge "$PR_URL" --squash --delete-branch
echo "PR merged."

# Switch back to main and pull the merge commit
git checkout main
git pull origin main

# Tag the merge commit and push the tag
git tag "v$VERSION"
git push origin "v$VERSION"

# Clean up local release branch if it still exists
git branch -d "$RELEASE_BRANCH" 2>/dev/null || true

echo ""
echo "Release v$VERSION pushed. The GitHub Actions workflow will handle the rest."
