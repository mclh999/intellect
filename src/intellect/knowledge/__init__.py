"""3 知识管理模块（DESIGN 2.2）：正式知识、版本、来源关联、分类、别名。"""

from intellect.knowledge._common import KnowledgeManagementError
from intellect.knowledge.aliases import Alias, AliasService
from intellect.knowledge.categories import Category, CategorySeed, CategoryService

__all__ = ["Alias", "AliasService", "Category", "CategorySeed", "CategoryService", "KnowledgeManagementError"]
